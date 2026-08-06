"""Fetch historical bars from IBKR and write them in the project schema.

REQUIRES a running TWS or IB Gateway with the API enabled
(Configure -> API -> Settings -> Enable ActiveX and Socket Clients).
  * Paper TWS socket: 7497   * Live TWS: 7496
  * Paper Gateway:    4002   * Live Gateway: 4001

This uses the IBKR adapter's HistoricInteractiveBrokersClient shipped with
nautilus_trader 1.229 (nautilus-ibapi 10.45.1). That adapter is installed ONLY
in the project's 3.12 venv, so run this with that interpreter, e.g.:

    python -m quant.data.ibkr_fetch --tickers BTC ETH SOL XRP DOGE ADA AVAX LINK LTC BCH \
       --years 5 --port 7497 --out quant/data/ibkr_bars.csv

    python -m quant.data.ibkr_fetch --tickers BTC ETH LTC BCH \
       --years 5 --port 7497 --out quant/data/ibkr_bars.csv
(from the workspace root -- the parent of the `quant/` package). Plain `python`
is not installed here and the system `python3` (3.14) has no nautilus_trader.

Crypto venue note
-----------------
IBKR routes US spot crypto through **Zero Hash** (exchange ``ZEROHASH``); it
migrated off Paxos, so ``PAXOS`` now errors on data requests. nautilus 1.229
still hardcodes PAXOS, so ``quant.data.ib_compat.register_zerohash_crypto()``
patches the adapter to accept ZEROHASH. The default ``--price-type MID`` requests
``MIDPOINT`` bars; ``--price-type LAST`` requests ``AGGTRADES`` trade prints
(the shim maps LAST->AGGTRADES because IBKR rejects plain ``TRADES`` for crypto).

Bar size note
-------------
IBKR only supports hourly bar sizes of 1, 2, 3, 4, or 8 hours (plus 1 day). The
default is 4 hours (natively supported). For any ``--bar-hours`` that isn't
natively supported (e.g. 12) we fetch the largest supported divisor and resample
OHLCV to the requested window.

History depth note
------------------
``--years N`` is a *ceiling*, not a guarantee. IBKR returns only as much history
as its Zero Hash feed holds for each contract, which varies by coin:

  * The original Paxos-era coins (BTC, ETH, LTC, BCH) have the deepest 4h
    history (roughly the last ~3 years -- IBKR's intraday-crypto retention).
  * Coins listed later on Zero Hash (SOL, XRP, DOGE, ADA, AVAX, LINK, ...) have
    shorter horizons and begin only at their feed-start date, no matter how far
    back ``--years`` asks.

This is a data-availability limit, not a fetch bug: nautilus already issues a
single max-duration request per coin. When the earliest returned bar is well
after the requested start, the per-ticker log prints a ``[!]`` note saying so.
For deeper history on the newer coins you need a different data source (their
IBKR history simply does not exist earlier).

Output CSV matches data_loader / backtest schema:
    timestamp,ticker,open,high,low,close,volume
``timestamp`` is ``YYYY-MM-DD HH:MM:SS`` (UTC) for sub-daily bars, else a date.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from pathlib import Path

import pandas as pd

from quant.data.ib_compat import CRYPTO_VENUE, register_zerohash_crypto

# Natively supported IBKR hourly bar sizes (see bar_spec_to_bar_size in nautilus).
_NATIVE_HOUR_STEPS = (8, 4, 3, 2, 1)  # descending, for "largest divisor" search

# ibapi MarketDataTypeEnum values (int). DELAYED_FROZEN is the documented option
# for accounts WITHOUT a live data subscription (nautilus IB config docs).
_MARKET_DATA_TYPES = {
    "REALTIME": 1,
    "FROZEN": 2,
    "DELAYED": 3,
    "DELAYED_FROZEN": 4,
}

# ~366 days/year buffers leap years so `--years N` really covers N calendar years.
_DAYS_PER_YEAR = 366

# If a coin's earliest returned bar is more than this many days after the
# requested start, the request hit IBKR's data horizon rather than our window --
# newer Zero Hash listings simply have no history that far back. We flag it so a
# short series reads as "IBKR has no earlier data" instead of "fetch is broken".
_HORIZON_SHORTFALL_DAYS = 5


def _plan_bars(bar_hours: int, price_type: str) -> tuple[str, int | None]:
    """Return (bar_specification, resample_hours_or_None) for a target window.

    If ``bar_hours`` is natively supported we request it directly. Otherwise we
    request the largest native hourly size that evenly divides ``bar_hours`` and
    resample to ``bar_hours`` afterwards.
    """
    if bar_hours < 1:
        raise SystemExit("--bar-hours must be >= 1")

    if bar_hours in _NATIVE_HOUR_STEPS:
        return f"{bar_hours}-HOUR-{price_type}", None

    for step in _NATIVE_HOUR_STEPS:
        if bar_hours % step == 0:
            return f"{step}-HOUR-{price_type}", bar_hours

    raise SystemExit(
        f"--bar-hours {bar_hours} cannot be built from IBKR sizes {_NATIVE_HOUR_STEPS}. "
        "Pick a multiple of one of them (e.g. 12 = 3x4h, 24 = 3x8h)."
    )


def _resample(df: pd.DataFrame, bar_hours: int) -> pd.DataFrame:
    """Aggregate fetched bars up to ``bar_hours``-wide OHLCV bars per ticker."""
    frames = []
    for sym, g in df.set_index("timestamp").sort_index().groupby("ticker"):
        agg = (
            g.resample(f"{bar_hours}h", label="left", closed="left")
            .agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
            )
            .dropna(subset=["open"])
        )
        agg["ticker"] = sym
        frames.append(agg.reset_index())
    return pd.concat(frames, ignore_index=True)


async def _fetch(
    tickers, years, host, port, client_id, exchange, price_type, bar_hours,
    market_data_type, request_timeout, asset_class="crypto", primary_exchange="",
):
    is_equity = asset_class == "equity"
    if not is_equity:
        # Patch the nautilus 1.229 IB adapter to treat ZEROHASH as a crypto
        # venue. Must run before the adapter builds instruments from our
        # contracts. Entirely crypto-specific -- STK contracts never touch the
        # CRYPTO secType decode path this patches, so skip it for equities.
        register_zerohash_crypto(exchange)

    # Imported lazily so the rest of the repo runs without the IB adapter /
    # without TWS. On Python 3.14 these imports may fail -- see module docstring.
    from nautilus_trader.adapters.interactive_brokers.common import IBContract
    from nautilus_trader.adapters.interactive_brokers.historical.client import (
        HistoricInteractiveBrokersClient,
    )

    bar_spec, resample_hours = _plan_bars(bar_hours, price_type)
    print(f"Requesting {bar_spec} bars" + (f", resampling to {bar_hours}h" if resample_hours else ""))

    # market_data_type is issued via reqMarketDataType on connect. DELAYED_FROZEN
    # lets accounts without a crypto data subscription still pull bars (if IBKR
    # offers delayed crypto data); REALTIME requires the paid subscription.
    client = HistoricInteractiveBrokersClient(
        host=host,
        port=port,
        client_id=client_id,
        market_data_type=_MARKET_DATA_TYPES[market_data_type],
    )
    await client.connect()

    end = dt.datetime.now()  # naive; nautilus localizes via tz_name
    start = end - dt.timedelta(days=int(years * _DAYS_PER_YEAR))

    rows = []
    for sym in tickers:
        if is_equity:
            # Stocks/ETFs: STK contract, SMART-routed. primary_exchange is
            # optional -- IBKR's SMART router usually resolves unambiguous US
            # large-cap tickers without it; pass --primary-exchange (e.g.
            # "ARCA"/"NASDAQ") if a symbol needs disambiguation.
            contract = IBContract(
                secType="STK",
                symbol=sym,
                exchange="SMART",
                currency="USD",
                primaryExchange=primary_exchange or None,
            )
        else:
            # Spot crypto on IBKR is a CRYPTO contract routed to Zero Hash,
            # quoted in USD (e.g. BTC/USD). No primaryExchange / SMART routing
            # as with stocks.
            contract = IBContract(
                secType="CRYPTO",
                symbol=sym,
                exchange=exchange,
                currency="USD",
            )
        # Equities have real market hours (use_rth=True); crypto is 24/7 (there
        # are no "regular trading hours" to restrict to).
        try:
            bars = await client.request_bars(
                bar_specifications=[bar_spec],
                contracts=[contract],
                start_date_time=start,
                end_date_time=end,
                tz_name="America/New_York",  # required by nautilus 1.229
                use_rth=is_equity,
                # Per-segment timeout. nautilus walks the window year-by-year; a
                # segment before a coin's Zero Hash feed-start returns IBKR error
                # 162 "no data" but never resolves its future, so it waits out
                # the full timeout. The 120s default makes newer coins crawl (2
                # min per empty pre-listing segment); a shorter value skips those
                # gaps fast without affecting in-horizon segments.
                timeout=request_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - surface per-symbol failure, keep going
            print(f"{sym}: request failed ({type(exc).__name__}: {exc})")
            continue

        bar_times: list[pd.Timestamp] = []
        for b in bars:
            ts = pd.Timestamp(b.ts_event, unit="ns", tz="UTC")
            bar_times.append(ts)
            rows.append(
                {
                    "timestamp": ts,
                    "ticker": sym,
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": float(b.volume),
                }
            )

        # Report the actual date span, not just a bar count -- a short series on
        # a newer coin is IBKR's data horizon, not a fetch failure. A "5 Y"
        # request that comes back starting in 2025 proves IBKR/Zero Hash has no
        # earlier history for this contract (see module docstring / CLAUDE.md).
        if not bar_times:
            print(
                f"{sym}: 0 bars -- contract qualified but IBKR returned no data for "
                f"the window (check crypto data permissions / --market-data-type)"
            )
            continue

        first, last = min(bar_times).date(), max(bar_times).date()
        shortfall = (first - start.date()).days
        note = ""
        if shortfall > _HORIZON_SHORTFALL_DAYS:
            note = (
                f"  [!] earliest IBKR bar is {first}, {shortfall} days after the "
                f"requested {start.date()} -- IBKR/Zero Hash has no {sym} history "
                f"before {first} (newer listings have shorter horizons; not a fetch error)"
            )
        print(f"{sym}: {len(bar_times)} raw bars  {first} -> {last}{note}")

    # NOTE: HistoricInteractiveBrokersClient (1.229) has no disconnect();
    # the connection closes with the event loop.
    if not rows:
        raise SystemExit(
            "No bars returned for any ticker. If you saw IBKR error 162 'No market "
            "data permissions for ZEROHASH CRYPTO', your account lacks a crypto data "
            "subscription -- either subscribe in IBKR Client Portal (Settings -> User "
            "Settings -> Market Data Subscriptions -> add the crypto/Zero Hash feed) "
            "or retry with '--market-data-type DELAYED_FROZEN'. Otherwise check the "
            "TWS/Gateway API is enabled and '--price-type MID' if there is no trades feed."
        )

    out = pd.DataFrame(rows)
    # IBKR batch requests can overlap at boundaries -> drop exact dupes first.
    out = out.drop_duplicates(subset=["timestamp", "ticker"])
    if resample_hours:
        out = _resample(out, resample_hours)

    out = out.sort_values(["timestamp", "ticker"]).reset_index(drop=True)
    fmt = "%Y-%m-%d" if bar_hours % 24 == 0 else "%Y-%m-%d %H:%M:%S"
    out["timestamp"] = out["timestamp"].dt.strftime(fmt)
    return out[["timestamp", "ticker", "open", "high", "low", "close", "volume"]]


async def _fetch_and_merge(
    csv_path, tickers, years, host, port, client_id, exchange, price_type,
    bar_hours, market_data_type, request_timeout, asset_class="crypto",
    primary_exchange="",
) -> list[str]:
    """Fetch missing tickers at the CSV's existing frequency and merge them."""
    columns = ["timestamp", "ticker", "open", "high", "low", "close", "volume"]
    existing = pd.read_csv(csv_path) if Path(csv_path).exists() else pd.DataFrame(columns=columns)
    existing_frequency = _infer_bar_hours(existing)
    if existing_frequency is not None:
        if bar_hours is not None and int(bar_hours) != existing_frequency:
            raise ValueError(
                f"Cannot merge missing tickers at {bar_hours}h into a "
                f"{existing_frequency}h CSV. Use replace_bars() to change frequency."
            )
        # Missing-ticker fetches are additive only.  A frequency change must
        # use replace_bars(), which rewrites the entire file atomically.
        bar_hours = existing_frequency
    elif bar_hours is None:
        bar_hours = 4
    have = set(existing["ticker"].unique()) if len(existing) else set()
    missing = [t for t in tickers if t not in have]
    if not missing:
        return []

    print(f"[ensure_tickers] fetching missing tickers via IBKR: {missing}")
    fetched = await _fetch(
        missing, years, host, port, client_id, exchange, price_type, bar_hours,
        market_data_type, request_timeout, asset_class=asset_class,
        primary_exchange=primary_exchange,
    )
    merged = pd.concat([existing, fetched], ignore_index=True)
    merged = merged.drop_duplicates(subset=["timestamp", "ticker"], keep="last")
    merged = merged.sort_values(["timestamp", "ticker"]).reset_index(drop=True)
    merged.to_csv(csv_path, index=False)
    print(
        f"[ensure_tickers] merged {len(fetched):,} new rows for {missing} -> {csv_path} "
        f"(file now has {len(merged):,} rows, {merged['ticker'].nunique()} tickers)"
    )
    return missing


def ensure_tickers(
    csv_path: str,
    tickers: list[str],
    asset_class: str = "crypto",
    years: int = 5,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 1,
    exchange: str | None = None,
    price_type: str = "MID",
    bar_hours: int | None = None,
    market_data_type: str = "REALTIME",
    request_timeout: int = 30,
    primary_exchange: str = "",
) -> list[str]:
    """Fetch missing tickers using the frequency already present in `csv_path`.

    ``bar_hours`` is retained as an optional compatibility argument, but when
    a CSV already exists its inferred frequency always wins.  Use
    ``replace_bars`` to intentionally change frequency.
    """
    resolved_exchange = exchange or ("SMART" if asset_class == "equity" else CRYPTO_VENUE)
    return asyncio.run(
        _fetch_and_merge(
            csv_path, tickers, years, host, port, client_id, resolved_exchange,
            price_type, bar_hours, market_data_type, request_timeout,
            asset_class=asset_class, primary_exchange=primary_exchange,
        )
    )


def _infer_bar_hours(df: pd.DataFrame) -> int | None:
    """Infer the CSV's bar width from the median positive timestamp delta."""
    if df.empty or "timestamp" not in df.columns:
        return None
    timestamps = pd.to_datetime(df["timestamp"], format="mixed", utc=True, errors="raise")
    deltas = timestamps.sort_values().diff().dt.total_seconds().div(3600)
    deltas = deltas[deltas > 0]
    if deltas.empty:
        return None
    return max(1, int(round(float(deltas.median()))))


def replace_bars(
    csv_path: str,
    tickers: list[str],
    asset_class: str = "crypto",
    years: int = 5,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 1,
    exchange: str | None = None,
    price_type: str = "MID",
    bar_hours: int = 4,
    market_data_type: str = "REALTIME",
    request_timeout: int = 30,
    primary_exchange: str = "",
) -> int:
    """Fetch the requested universe and replace the CSV at one frequency."""
    resolved_exchange = exchange or ("SMART" if asset_class == "equity" else CRYPTO_VENUE)
    fetched = asyncio.run(
        _fetch(
            tickers, years, host, port, client_id, resolved_exchange, price_type,
            bar_hours, market_data_type, request_timeout,
            asset_class=asset_class, primary_exchange=primary_exchange,
        )
    )
    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    temp_path = Path(f"{csv_path}.tmp")
    fetched.to_csv(temp_path, index=False)
    temp_path.replace(csv_path)
    print(f"[replace_bars] replaced {csv_path} with {len(fetched):,} rows at {bar_hours}h")
    return len(fetched)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="+", required=True)
    p.add_argument(
        "--asset-class",
        choices=("crypto", "equity"),
        default="crypto",
        help="crypto: CRYPTO contract on Zero Hash, 24/7 (use_rth=False). "
        "equity: STK contract, SMART-routed, regular trading hours (use_rth=True).",
    )
    p.add_argument("--years", type=int, default=5)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)  # paper TWS
    p.add_argument("--client-id", type=int, default=1)
    p.add_argument(
        "--exchange",
        default=None,
        help="IBKR crypto exchange/venue (default ZEROHASH; use PAXOS for legacy "
        "accounts). Ignored for --asset-class equity (always SMART).",
    )
    p.add_argument(
        "--primary-exchange",
        default="",
        help="Optional primaryExchange for equity STK contracts (e.g. ARCA, "
        "NASDAQ) to disambiguate a ticker under SMART routing. Ignored for crypto.",
    )
    p.add_argument(
        "--price-type",
        choices=("LAST", "MID"),
        default="MID",
        help="MID=MIDPOINT bars (default); LAST=AGGTRADES trade prints.",
    )
    p.add_argument(
        "--bar-hours",
        type=int,
        default=4,
        help="Target bar width in hours (default 4; resampled from a native IBKR size).",
    )
    p.add_argument(
        "--market-data-type",
        choices=tuple(_MARKET_DATA_TYPES),
        default="REALTIME",
        help="REALTIME needs a paid data subscription; DELAYED_FROZEN works without one.",
    )
    p.add_argument(
        "--request-timeout",
        type=int,
        default=30,
        help=(
            "Per-segment IBKR request timeout in seconds (default 30). nautilus "
            "walks the window year-by-year; pre-listing segments return error 162 "
            "'no data' and otherwise stall the full timeout. Lower = newer coins "
            "skip their empty pre-listing gaps faster. Raise if legitimate deep "
            "segments time out before IBKR responds."
        ),
    )
    p.add_argument("--out", default="quant/data/ibkr_bars.csv")
    args = p.parse_args()
    exchange = args.exchange or ("SMART" if args.asset_class == "equity" else CRYPTO_VENUE)

    df = asyncio.run(
        _fetch(
            args.tickers,
            args.years,
            args.host,
            args.port,
            args.client_id,
            exchange,
            args.price_type,
            args.bar_hours,
            args.market_data_type,
            args.request_timeout,
            asset_class=args.asset_class,
            primary_exchange=args.primary_exchange,
        )
    )
    df.to_csv(args.out, index=False)
    print(f"Wrote {len(df):,} rows -> {args.out}")


if __name__ == "__main__":
    main()
