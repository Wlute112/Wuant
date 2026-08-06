"""Paper / live runner -- SAME MLStrategy, only the engine + venue change.

This is the backtest->paper->live promotion path. The strategy class, the
risk rules, and the prediction engine are byte-for-byte identical to the
backtest; only the TradingNode + IBKR client config differ here.

  PAPER:  TWS/Gateway logged into a PAPER account, port 7497 (TWS) / 4002 (GW)
  LIVE:   TWS/Gateway logged into a LIVE account,  port 7496 (TWS) / 4001 (GW)
          -> pass --live to flip the default port and require explicit opt-in.

Requires the IBKR adapter to import successfully. On Python 3.14 that may fail;
use a 3.12 venv for paper/live (see README). Backtesting does NOT need this.

Crypto tickers are base-asset codes (BTC, ETH, SOL, ...) traded against USD on
IBKR's Zero Hash venue. Equity tickers are SMART-routed US stocks/ETFs and use
regular trading hours, whole-share quantities, and LAST-price daily bars.

Usage:
    # Redis must be running first (macOS: brew services start redis)
    # paper (default)
    TWS_ACCOUNT=DU1234567 python -m quant.run.run_live \
        --tickers BTC ETH SOL --port 7497 \
        --params quant/optimize/best_params.json
    # equity paper
    TWS_ACCOUNT=DU1234567 python -m quant.run.run_live \
        --asset-class equity --tickers SPY QQQ --port 7497
    # live (explicit, dangerous)
    python -m quant.run.run_live --tickers BTC ETH SOL --live --port 7496
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from quant.data.ib_compat import (
    crypto_instrument_id,
    register_ibkr_execution_fixes,
    register_zerohash_crypto,
)

PAPER_PORTS = frozenset({7497, 4002})
LIVE_PORTS = frozenset({7496, 4001})
ASSET_CLASSES = ("crypto", "equity")


def equity_instrument_id(ticker: str, primary_exchange: str = "") -> str:
    return f"{ticker.upper()}.{primary_exchange.upper() or 'SMART'}"


def instrument_ids_for_asset(
    tickers: list[str],
    asset_class: str,
    primary_exchange: str = "",
) -> list[str]:
    if asset_class == "crypto":
        return [crypto_instrument_id(t) for t in tickers]
    if asset_class == "equity":
        return [equity_instrument_id(t, primary_exchange) for t in tickers]
    raise ValueError(f"unsupported asset class: {asset_class!r}")


# IBKR only supports these hourly bar sizes for a continuing (EXTERNAL,
# keepUpToDate) live subscription -- same set data/ibkr_fetch.py's historical
# fetch treats as natively fetchable (see its _NATIVE_HOUR_STEPS). Anything
# else there gets resampled client-side after the fact, which isn't possible
# for a live stream, so live bar-hours is restricted to this set (or 24/None
# for the original daily default).
IBKR_LIVE_BAR_HOURS = (1, 2, 3, 4, 8)


def bar_type_suffix_for_asset(asset_class: str, bar_hours: int | None = None) -> str:
    if asset_class == "crypto":
        # Crypto needs MID because IBKR rejects continuing AGGTRADES/LAST
        # subscriptions (error 321).
        price = "MID"
    elif asset_class == "equity":
        price = "LAST"
    else:
        raise ValueError(f"unsupported asset class: {asset_class!r}")
    if bar_hours is None or bar_hours == 24:
        return f"-1-DAY-{price}-EXTERNAL"
    if bar_hours not in IBKR_LIVE_BAR_HOURS:
        raise ValueError(
            f"--bar-hours {bar_hours} isn't a live-subscribable IBKR bar size "
            f"{IBKR_LIVE_BAR_HOURS} (or 24 for daily). Pick one of those."
        )
    return f"-{bar_hours}-HOUR-{price}-EXTERNAL"


def validate_mode_port(is_live: bool, port: int) -> None:
    if is_live and port in PAPER_PORTS:
        raise ValueError(
            f"Refusing live trading on known paper port {port}. "
            "Use 7496 (TWS) or 4001 (Gateway)."
        )
    if not is_live and port in LIVE_PORTS:
        raise ValueError(
            f"Refusing paper trading on known live port {port}. "
            "Use 7497 (TWS) or 4002 (Gateway)."
        )


def load_params(path: str | None) -> tuple[dict, int | None]:
    """Returns (strategy_params, ibkr_bar_hours).

    ``ibkr_bar_hours`` is a runner-level setting (which live bar width to
    subscribe at), not an MLStrategyConfig field, so it's read straight off
    the top-level payload rather than going through the ``allowed`` filter
    below -- see optimize.py's ``--ibkr-bar-hours`` / the run artifact's
    ``ibkr_bar_hours`` key.
    """
    if path is None:
        return {}, None
    with Path(path).expanduser().open() as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError("--params must contain a JSON object")

    from nautilus_trader.config import StrategyConfig
    from quant.strategies.ml_strategy import MLStrategyConfig

    runtime_owned = {
        "instrument_ids",
        "bar_type_suffix",
        "account_id",
        "allow_short_positions",
        "request_historical_bars",
        "use_allocated_equity",
    }
    allowed = (
        set(MLStrategyConfig.__struct_fields__)
        - set(StrategyConfig.__struct_fields__)
        - runtime_owned
    )

    # Accept all formats exposed by the project:
    #   * a flat strategy-parameter object;
    #   * optimize/best_params.json, with tuned values under "params";
    #   * a dashboard run artifact, with tuned values under "best_params".
    # Top-level structural settings are retained, while run metadata is
    # discarded before MLStrategyConfig sees it.
    nested = payload
    for field in ("params", "best_params"):
        if field not in payload or payload[field] is None:
            continue
        if not isinstance(payload[field], dict):
            raise ValueError(f"--params JSON field {field!r} must be an object")
        nested = payload[field]
        break

    structural = {key: value for key, value in payload.items() if key in allowed}
    tuned = {key: value for key, value in nested.items() if key in allowed}
    ibkr_bar_hours = payload.get("ibkr_bar_hours")
    return {**structural, **tuned}, ibkr_bar_hours


def build_node(
    tickers,
    host,
    port,
    client_id,
    is_live,
    params,
    *,
    account_id: str,
    persistence: bool = True,
    redis_host: str = "127.0.0.1",
    redis_port: int = 6379,
    redis_username: str | None = None,
    redis_password: str | None = None,
    asset_class: str = "crypto",
    primary_exchange: str = "",
    allow_short_positions: bool = False,
    bar_hours: int | None = None,
):
    register_ibkr_execution_fixes()
    if asset_class == "crypto":
        # Teach nautilus 1.229 that ZEROHASH is a crypto venue before it
        # decodes load_ids or subscribes to bars.
        register_zerohash_crypto()

    from nautilus_trader.adapters.interactive_brokers.common import IBContract
    from nautilus_trader.adapters.interactive_brokers.config import (
        InteractiveBrokersDataClientConfig,
        InteractiveBrokersExecClientConfig,
        InteractiveBrokersInstrumentProviderConfig,
    )
    from nautilus_trader.adapters.interactive_brokers.factories import (
        InteractiveBrokersLiveDataClientFactory,
        InteractiveBrokersLiveExecClientFactory,
    )
    from nautilus_trader.config import (
        CacheConfig,
        DatabaseConfig,
        LoggingConfig,
        TradingNodeConfig,
    )
    from nautilus_trader.live.config import LiveExecEngineConfig
    from nautilus_trader.live.node import TradingNode

    from quant.strategies.ml_strategy import MLStrategy, MLStrategyConfig

    instrument_ids = instrument_ids_for_asset(tickers, asset_class, primary_exchange)
    if asset_class == "equity" and not primary_exchange:
        # Qualify normal SMART contracts while forcing stable TICKER.SMART
        # Nautilus IDs. Each client retains IBKR's qualified contract, including
        # its actual primary exchange, for subscriptions and order routing.
        contracts = frozenset(
            IBContract(
                secType="STK",
                symbol=t.upper(),
                exchange="SMART",
                currency="USD",
            )
            for t in tickers
        )
        provider = InteractiveBrokersInstrumentProviderConfig(
            load_contracts=contracts,
            symbol_to_mic_venue={t.upper(): "SMART" for t in tickers},
        )
    else:
        provider = InteractiveBrokersInstrumentProviderConfig(
            load_ids=frozenset(instrument_ids),
        )

    data_cfg = InteractiveBrokersDataClientConfig(
        ibg_host=host,
        ibg_port=port,
        ibg_client_id=client_id,
        use_regular_trading_hours=asset_class == "equity",
        instrument_provider=provider,
    )
    exec_cfg = InteractiveBrokersExecClientConfig(
        ibg_host=host,
        ibg_port=port,
        ibg_client_id=client_id,
        account_id=account_id,
        instrument_provider=provider,
    )

    cache_cfg = None
    if persistence:
        cache_cfg = CacheConfig(
            database=DatabaseConfig(
                type="redis",
                host=redis_host,
                port=redis_port,
                username=redis_username,
                password=redis_password,
                number_of_retries=3,
            ),
            persist_account_events=True,
            buffer_interval_ms=100,
            flush_on_start=False,
            drop_instruments_on_reset=False,
        )

    node_cfg = TradingNodeConfig(
        trader_id=(
            f"{'LIVE' if is_live else 'PAPER'}-{asset_class.upper()}-001"
        ),
        logging=LoggingConfig(log_level="INFO"),
        cache=cache_cfg,
        exec_engine=LiveExecEngineConfig(
            load_cache=persistence,
            reconciliation=True,
            snapshot_orders=persistence,
            snapshot_positions=persistence,
            snapshot_positions_interval_secs=60.0 if persistence else None,
        ),
        # The strategy is added programmatically below, after TradingNode's
        # constructor performs its automatic load pass. We explicitly load it
        # after registration instead.
        load_state=False,
        save_state=persistence,
        data_clients={"IB": data_cfg},
        exec_clients={"IB": exec_cfg},
    )

    node = TradingNode(config=node_cfg)
    node.add_data_client_factory("IB", InteractiveBrokersLiveDataClientFactory)
    node.add_exec_client_factory("IB", InteractiveBrokersLiveExecClientFactory)

    strategy_params = {
        "starting_equity": 5000.0,
        **(params or {}),
        "account_id": f"IB-{account_id}",
        "request_historical_bars": True,
        "use_allocated_equity": bool((params or {}).get("use_allocated_equity", False)),
        "allow_short_positions": allow_short_positions,
    }
    strat_cfg = MLStrategyConfig(
        instrument_ids=instrument_ids,
        bar_type_suffix=bar_type_suffix_for_asset(asset_class, bar_hours),
        **strategy_params,
    )
    strategy = MLStrategy(strat_cfg)
    node.trader.add_strategy(strategy)
    if persistence:
        node.trader.load()
    node.build()
    return node


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="+", required=True)
    p.add_argument("--asset-class", choices=ASSET_CLASSES, default="crypto")
    p.add_argument(
        "--primary-exchange",
        default="",
        help="optional equity primary exchange (for example ARCA or NASDAQ)",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)  # 7497 paper TWS
    p.add_argument("--client-id", type=int, default=1)
    p.add_argument(
        "--account-id",
        default=os.environ.get("TWS_ACCOUNT"),
        help="IBKR account id (or set TWS_ACCOUNT); paper accounts usually start with DU",
    )
    p.add_argument("--params", help="Optuna best_params/structural JSON")
    p.add_argument(
        "--bar-hours",
        type=int,
        default=None,
        help="Live bar width in hours (1, 2, 3, 4, or 8; omit or pass 24 for "
        "1-day bars, the original default). If omitted and --params carries "
        "an ibkr_bar_hours value (e.g. an Optuna run artifact that used "
        "--fetch-missing), that value is used instead.",
    )
    p.add_argument(
        "--cash",
        type=float,
        default=None,
        help="optional allocated equity; when omitted, use the connected IBKR account balance",
    )
    p.add_argument("--redis-host", default="127.0.0.1")
    p.add_argument("--redis-port", type=int, default=6379)
    p.add_argument("--redis-username", default=os.environ.get("NAUTILUS_REDIS_USERNAME"))
    p.add_argument(
        "--redis-password-env",
        default="NAUTILUS_REDIS_PASSWORD",
        help="environment variable containing the Redis password",
    )
    p.add_argument(
        "--no-persistence",
        action="store_true",
        help="disable Redis state persistence (diagnostics only)",
    )
    p.add_argument("--live", action="store_true", help="connect to a LIVE account")
    p.add_argument(
        "--allow-shorts",
        action="store_true",
        help="allow short positions (equity only; off by default)",
    )
    args = p.parse_args()

    try:
        validate_mode_port(args.live, args.port)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not args.account_id:
        raise SystemExit("Missing IBKR account id: pass --account-id or set TWS_ACCOUNT.")
    if args.allow_shorts and args.asset_class != "equity":
        raise SystemExit("--allow-shorts is supported only with --asset-class equity.")
    try:
        params, params_bar_hours = load_params(args.params)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"Invalid --params file: {exc}") from exc
    if args.cash is not None:
        params["starting_equity"] = args.cash
        params["use_allocated_equity"] = True
    elif args.live:
        # Live keeps the explicit $5,000 allocation default unless the
        # operator passes --cash. Paper sessions use the broker balance.
        params["starting_equity"] = 5000.0
        params["use_allocated_equity"] = True
    else:
        params.pop("starting_equity", None)
        params["use_allocated_equity"] = False
    bar_hours = args.bar_hours if args.bar_hours is not None else params_bar_hours

    try:
        node = build_node(
            tickers=args.tickers,
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            is_live=args.live,
            params=params,
            account_id=args.account_id,
            persistence=not args.no_persistence,
            redis_host=args.redis_host,
            redis_port=args.redis_port,
            redis_username=args.redis_username,
            redis_password=os.environ.get(args.redis_password_env),
            asset_class=args.asset_class,
            primary_exchange=args.primary_exchange,
            allow_short_positions=args.allow_shorts,
            bar_hours=bar_hours,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
