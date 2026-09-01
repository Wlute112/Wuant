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
regular trading hours, whole-share quantities, and LAST-price completed bars.

Usage:
    # Redis must be running first (macOS: brew services start redis)
    # equity paper (IBKR paper does not support spot-crypto execution)
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
from quant.run.asset_profiles import get_asset_profile, strategy_defaults_for_asset
from quant.run.readiness import LiveCapitalDisabledError, assert_live_capital_enabled

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


def validate_asset_mode(is_live: bool, asset_class: str) -> None:
    if not is_live and asset_class == "crypto":
        raise ValueError(
            "IBKR paper accounts do not support spot-crypto execution. "
            "Use --asset-class equity for paper trading or run a crypto backtest."
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
        "telemetry_path",
        "telemetry_asset_class",
        "telemetry_mode",
        "telemetry_include_extended_hours",
        "operations_db_path",
        "operations_component_id",
        "external_supervisor_component",
        "require_external_supervisor",
        "order_tags",
        "execution_mode",
        "asset_class",
        "news_data_path",
        "entry_time_in_force",
        "enable_broker_protection",
        "risk_check_interval_secs",
        "require_session_schedule",
        "session_policy",
        "backtest_model_fit_end_ns",
        "backtest_trade_start_ns",
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


def load_params_metadata(path: str | None) -> dict:
    """Read compatibility metadata without treating it as strategy config."""
    if path is None:
        return {}
    with Path(path).expanduser().open() as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        return {}
    return {
        key: payload[key]
        for key in (
            "asset_class",
            "objective_metric",
            "include_extended_hours",
            "market_session",
            "bar_interval_minutes",
        )
        if key in payload
    }


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
    include_extended_hours: bool = False,
    telemetry_path: str = "",
    news_db_path: str = "",
    operations_db_path: str = "",
    operations_component_id: str = "",
    external_supervisor_component: str = "",
    require_external_supervisor: bool = False,
):
    if is_live:
        assert_live_capital_enabled()
    register_ibkr_execution_fixes()
    if asset_class == "crypto":
        # Teach nautilus 1.229 that ZEROHASH is a crypto venue before it
        # decodes load_ids or subscribes to bars.
        register_zerohash_crypto()

    from nautilus_trader.adapters.interactive_brokers.common import IBContract, IBOrderTags
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
    from nautilus_trader.model.enums import TimeInForce
    from nautilus_trader.model.identifiers import InstrumentId

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
        use_regular_trading_hours=(
            asset_class == "equity" and not include_extended_hours
        ),
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
        "telemetry_path": telemetry_path,
        "telemetry_asset_class": asset_class,
        "telemetry_mode": "live" if is_live else "paper",
        "telemetry_include_extended_hours": include_extended_hours,
        "news_data_path": news_db_path,
        "operations_db_path": operations_db_path,
        "operations_component_id": operations_component_id,
        "external_supervisor_component": external_supervisor_component,
        "require_external_supervisor": require_external_supervisor,
        "expected_bar_interval_secs": int(bar_hours) * 3600,
        "execution_mode": "live" if is_live else "paper",
        "asset_class": asset_class,
        "entry_time_in_force": "DAY" if asset_class == "equity" else "GTC",
        "enable_broker_protection": True,
        "risk_check_interval_secs": 1,
        "require_session_schedule": asset_class == "equity",
        "session_policy": (
            "EXTENDED_HOURS"
            if asset_class == "equity" and include_extended_hours
            else "RTH_ONLY"
        ),
        "order_tags": (
            (IBOrderTags(outsideRth=True).value,)
            if asset_class == "equity" and include_extended_hours
            else ()
        ),
    }
    strat_cfg = MLStrategyConfig(
        instrument_ids=instrument_ids,
        bar_type_suffix=bar_type_suffix_for_asset(asset_class, bar_hours),
        manage_stop=True,
        market_exit_interval_ms=100,
        market_exit_max_attempts=300,
        market_exit_time_in_force=TimeInForce.IOC,
        market_exit_reduce_only=True,
        external_order_claims=[InstrumentId.from_str(value) for value in instrument_ids],
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
    p.add_argument("--model-id", default="", help="immutable model-registry identifier")
    p.add_argument("--model-registry", default="quant/models/registry.sqlite3")
    p.add_argument(
        "--bar-hours",
        type=int,
        default=None,
        help="Live bar width in hours (1, 2, 3, 4, or 8; omit or pass 24 for "
        "1-day bars). If omitted and --params carries "
        "an ibkr_bar_hours value (e.g. an Optuna run artifact that used "
        "--fetch-missing), that value is used; otherwise the asset profile "
        "defaults to 4-hour crypto bars or 1-hour equity bars.",
    )
    p.add_argument(
        "--include-extended-hours",
        action="store_true",
        help="Equity only: include pre/post-market bars and allow supported "
        "orders outside RTH. Extended-hours liquidity and fills differ materially.",
    )
    p.add_argument("--telemetry-path", default="", help=argparse.SUPPRESS)
    p.add_argument(
        "--operations-db",
        default="",
        help="shared SQLite audit/control database for the independent risk supervisor",
    )
    p.add_argument("--operations-component-id", default="", help=argparse.SUPPRESS)
    p.add_argument("--external-supervisor-component", default="", help=argparse.SUPPRESS)
    p.add_argument("--require-external-supervisor", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--news-operations-component-id", default="", help=argparse.SUPPRESS)
    p.add_argument(
        "--news-db",
        default="quant/data/news.sqlite3",
        help="append-only RSS/IBKR news database used by the alpha feature",
    )
    p.add_argument("--news-client-id", type=int, default=30)
    p.add_argument("--news-rss-catalog", default="")
    p.add_argument("--news-rss-poll-seconds", type=int, default=120)
    p.add_argument("--news-provider", action="append", default=[])
    p.add_argument("--news-ollama-url", default="http://127.0.0.1:11434/api/generate")
    p.add_argument("--news-ollama-model", default="lfm2:24b")
    p.add_argument("--no-news", action="store_true")
    p.add_argument("--no-ibkr-news", action="store_true")
    p.add_argument("--no-rss-news", action="store_true")
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
        validate_asset_mode(args.live, args.asset_class)
        if args.live:
            assert_live_capital_enabled()
    except (ValueError, LiveCapitalDisabledError) as exc:
        raise SystemExit(str(exc)) from exc
    if not args.account_id:
        raise SystemExit("Missing IBKR account id: pass --account-id or set TWS_ACCOUNT.")
    if args.allow_shorts:
        raise SystemExit(
            "Short selling is disabled until shortability, borrow, SSR, margin, "
            "recall, and forced-buy-in controls are implemented."
        )
    if not args.no_news and not args.no_ibkr_news and args.news_client_id == args.client_id:
        raise SystemExit("--news-client-id must differ from the TradingNode --client-id")
    params_source = args.params
    if args.model_id:
        if args.params:
            raise SystemExit("--model-id and --params are mutually exclusive")
        from quant.ops.model_registry import ModelRegistry

        registry = ModelRegistry(args.model_registry)
        try:
            params_source = registry.params_path(
                args.model_id,
                require_approved=args.live,
            )
        except (KeyError, ValueError) as exc:
            raise SystemExit(f"Invalid model registry selection: {exc}") from exc
        finally:
            registry.close()
    try:
        loaded_params, params_bar_hours = load_params(params_source)
        params_metadata = load_params_metadata(params_source)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"Invalid --params file: {exc}") from exc
    params_asset_class = params_metadata.get("asset_class")
    if params_asset_class and params_asset_class != args.asset_class:
        raise SystemExit(
            f"Params profile mismatch: file was optimized for {params_asset_class}, "
            f"but this run selected {args.asset_class}."
        )
    params_extended = params_metadata.get("include_extended_hours")
    if params_extended is not None and bool(params_extended) != args.include_extended_hours:
        raise SystemExit(
            "Params session mismatch: --include-extended-hours must match the "
            "historical session used by the params file."
        )
    params_bar_minutes = params_metadata.get("bar_interval_minutes")
    bar_hours = args.bar_hours if args.bar_hours is not None else params_bar_hours
    if bar_hours is None:
        bar_hours = int(get_asset_profile(args.asset_class)["defaults"]["bar_hours"])
    if params_bar_minutes is not None and int(params_bar_minutes) != int(bar_hours) * 60:
        raise SystemExit(
            f"Params cadence mismatch: file was trained on {params_bar_minutes}-minute bars, "
            f"but this run selected {int(bar_hours) * 60}-minute bars."
        )
    params = {
        **strategy_defaults_for_asset(
            args.asset_class,
            bar_hours,
            include_extended_hours=args.include_extended_hours,
        ),
        **loaded_params,
    }
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
    params["use_news_features"] = not args.no_news and bool(
        params.get("use_news_features", True)
    )
    if args.include_extended_hours and args.asset_class != "equity":
        raise SystemExit("--include-extended-hours is supported only with --asset-class equity.")

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
            include_extended_hours=args.include_extended_hours,
            telemetry_path=args.telemetry_path,
            news_db_path=args.news_db if params["use_news_features"] else "",
            operations_db_path=args.operations_db,
            operations_component_id=args.operations_component_id,
            external_supervisor_component=args.external_supervisor_component,
            require_external_supervisor=args.require_external_supervisor,
        )
    except (ValueError, LiveCapitalDisabledError) as exc:
        raise SystemExit(str(exc)) from exc
    news_service = None
    if params["use_news_features"]:
        from quant.news.service import NewsService, NewsServiceConfig

        news_service = NewsService(
            NewsServiceConfig(
                db_path=args.news_db,
                tickers=tuple(args.tickers),
                asset_class=args.asset_class,
                rss_enabled=not args.no_rss_news,
                rss_catalog_path=args.news_rss_catalog,
                rss_poll_seconds=args.news_rss_poll_seconds,
                ibkr_enabled=not args.no_ibkr_news,
                ibkr_host=args.host,
                ibkr_port=args.port,
                ibkr_client_id=args.news_client_id,
                ibkr_provider_allowlist=tuple(args.news_provider),
                ollama_url=args.news_ollama_url,
                ollama_model=args.news_ollama_model,
                operations_db_path=args.operations_db,
                operations_component_id=args.news_operations_component_id,
            )
        )
        news_service.start()
    try:
        node.run()
    finally:
        node.dispose()
        if news_service is not None:
            news_service.stop()


if __name__ == "__main__":
    main()
