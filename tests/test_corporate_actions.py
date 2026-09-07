from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from quant.data.instrument_identity import EquityIdentity, validate_equity_instrument
from quant.ops.corporate_actions import CorporateAction, CorporateActionRegistry
from quant.ops.state import OperationsStore

NOW = datetime(2026, 9, 9, 15, tzinfo=timezone.utc)


def identity(con_id=123, symbol="ABC", **changes):
    info = {"contract": {"conId": con_id, "symbol": symbol, "secType": "STK", "currency": "USD", "primaryExchange": "NYSE"}, "stockType": "COMMON"}
    info["contract"].update(changes)
    return EquityIdentity.from_info(info)


@pytest.fixture
def registry(tmp_path):
    store = OperationsStore(str(tmp_path / "ops.sqlite3"))
    registry = CorporateActionRegistry(store)
    registry.reconcile("IB-DU1", [identity()], broker_flat=True, now=NOW)
    yield registry
    store.close()


def action(kind="SPLIT", **changes):
    values = {"event_id": "evt-1", "kind": kind, "con_id": 123, "effective_at": NOW.isoformat(),
              "source_reference": "issuer notice 2026-09-09"}
    if kind == "SPLIT":
        values["split_ratio"] = "2"
    elif kind == "CASH_DIVIDEND":
        values["cash_per_share"] = "0.25"
    else:
        values["new_symbol"] = "XYZ"
    return CorporateAction(**{**values, **changes})


@pytest.mark.parametrize("changes", [{"conId": 0}, {"conId": True}, {"currency": "EUR"},
                                     {"secType": "CFD"}, {"secType": "OPT"}, {"primaryExchange": "LSE"}])
def test_equity_universe_rejects_unqualified_and_out_of_scope_instruments(changes):
    with pytest.raises(ValueError):
        identity(**changes)


def test_missing_or_unsupported_stock_type_fails_closed():
    for stock_type in ["", "WARRANT", "RIGHT", "ETN"]:
        instrument = SimpleNamespace(info={"contract": {"conId": 123, "symbol": "ABC", "secType": "STK", "currency": "USD", "primaryExchange": "NYSE"}, "stockType": stock_type})
        with pytest.raises(ValueError):
            validate_equity_instrument(instrument)


@pytest.mark.parametrize("changes", [{"split_ratio": 0}, {"split_ratio": "NaN"}, {"split_ratio": "Infinity"},
                                     {"split_ratio": 1}, {"cash_per_share": 5}, {"effective_at": "2026-09-09T12:00:00"}])
def test_invalid_events_are_rejected(changes):
    with pytest.raises(ValidationError):
        action(**changes)


@pytest.mark.parametrize("ratio", ["2", "0.1", "1.5"])
def test_split_is_idempotent_and_requires_flat_broker_then_new_warmup(registry, ratio):
    before = registry.generation("IB-DU1")
    event = action(split_ratio=ratio)
    first = registry.register_event("IB-DU1", event, "operator")
    assert registry.register_event("IB-DU1", event, "operator")["digest"] == first["digest"]
    with pytest.raises(ValueError, match="zero positions"):
        registry.reconcile("IB-DU1", [identity()], broker_flat=False, now=NOW)
    assert registry.events("IB-DU1")[0]["status"] == "PENDING"
    result = registry.reconcile("IB-DU1", [identity()], broker_flat=True, now=NOW)
    assert result["rebuild_required"]
    assert registry.generation("IB-DU1") != before
    assert registry.events("IB-DU1")[0]["status"] == "REBUILDING"
    registry.finish_rebuild("IB-DU1", {999})
    assert registry.events("IB-DU1")[0]["status"] == "REBUILDING"
    registry.finish_rebuild("IB-DU1", {123})
    registry.finish_rebuild("IB-DU1", {123})
    assert registry.events("IB-DU1")[0]["status"] == "APPLIED"
    assert registry.generation("IB-DU1") == result["generation"]
    assert registry.store.verify_audit_chain()[0]


@pytest.mark.parametrize("replacement", [None, 456])
def test_symbol_change_uses_conid_and_retains_old_alias(registry, replacement):
    registry.register_event("IB-DU1", action("SYMBOL_CHANGE", new_con_id=replacement), "operator")
    expected = replacement or 123
    assert registry.resolve("IB-DU1", "ABC")["con_id"] == expected
    with pytest.raises(ValueError):
        registry.reconcile("IB-DU1", [identity(expected, "WRONG")], broker_flat=True, now=NOW)
    registry.reconcile("IB-DU1", [identity(expected, "XYZ")], broker_flat=True, now=NOW)
    assert registry.resolve("IB-DU1", "ABC")["symbol"] == "XYZ"
    assert registry.resolve("IB-DU1", "XYZ")["con_id"] == expected
    registry.finish_rebuild("IB-DU1", {expected})
    with pytest.raises(ValueError, match="Ticker reuse"):
        registry.reconcile("IB-DU1", [identity(999, "ABC")], broker_flat=True, now=NOW)


def test_dividend_records_cash_basis_without_synthetic_credit(registry):
    registry.register_event("IB-DU1", action("CASH_DIVIDEND"), "operator")
    result = registry.reconcile("IB-DU1", [identity()], broker_flat=True, now=NOW)
    assert "no synthetic dividend credit" in result["events"][0]["result"]["cash_policy"]
    assert not any(event.event_type == "CASH_CREDIT" for event in registry.store.audit_events())


def test_future_unknown_conflicting_and_cancelled_events(registry):
    future = action(effective_at=NOW + timedelta(days=1))
    registry.register_event("IB-DU1", future, "operator")
    with pytest.raises(ValueError, match="effective"):
        registry.reconcile("IB-DU1", [identity()], broker_flat=True, now=NOW)
    with pytest.raises(ValueError, match="conflicts"):
        registry.register_event("IB-DU1", action(split_ratio=3), "operator")
    with pytest.raises(ValueError, match="qualified"):
        registry.register_event("IB-DU2", action(), "operator")
    registry.cancel("IB-DU1", "evt-1", "operator")
    assert registry.events("IB-DU1")[0]["status"] == "CANCELLED"
    assert not registry.reconcile("IB-DU1", [identity()], broker_flat=True, now=NOW)["rebuild_required"]


def test_unreviewed_symbol_change_and_duplicate_conids_block(registry):
    with pytest.raises(ValueError, match="Unreviewed"):
        registry.reconcile("IB-DU1", [identity(123, "XYZ")], broker_flat=True, now=NOW)
    with pytest.raises(ValueError, match="same broker conId"):
        registry.reconcile("IB-DU1", [identity(), identity()], broker_flat=True, now=NOW)


def test_journal_rolls_back_when_audit_fails(registry, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(registry.store, "append_event", fail)
    with pytest.raises(RuntimeError):
        registry.register_event("IB-DU1", action(), "operator")
    assert registry.events("IB-DU1") == []


def test_registry_and_pending_rebuild_survive_reopen(registry):
    registry.register_event("IB-DU1", action(), "operator")
    registry.reconcile("IB-DU1", [identity()], broker_flat=True, now=NOW)
    other_store = OperationsStore(registry.store.path)
    try:
        reopened = CorporateActionRegistry(other_store)
        assert reopened.resolve("IB-DU1", "ABC")["con_id"] == 123
        assert reopened.events("IB-DU1")[0]["status"] == "REBUILDING"
    finally:
        other_store.close()


def test_strategy_model_history_persists_by_conid():
    from quant.strategies.ml_strategy import MLStrategy, MLStrategyConfig
    from quant.strategies.risk import RiskManager
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.backtest.engine import BacktestEngine
    import json

    config = MLStrategyConfig(instrument_ids=["ABC.SMART"])
    iid = InstrumentId.from_str("ABC.SMART")
    def build():
        strategy = MLStrategy(config)
        strategy._risk = RiskManager(5000)
        strategy._equity_identities = {"ABC.SMART": identity()}
        strategy._identity_generation = "generation-1"
        strategy._bar_types[iid] = "unused"
        strategy._iid_by_raw["ABC.SMART"] = iid
        strategy._raw_by_iid[iid] = "ABC.SMART"
        return strategy
    strategy = build()
    strategy._closes[iid].extend([100.0, 101.0])
    strategy._highs[iid].extend([102.0, 103.0])
    strategy._lows[iid].extend([99.0, 100.0])
    saved = strategy.on_save()
    payload = json.loads(saved["ml_strategy.json"])
    assert payload["version"] == 9
    assert set(payload["instruments"]) == {"123"}
    assert payload["instrument_identities"]["123"]["symbol"] == "ABC"
    restored = build()
    engine = BacktestEngine()
    engine.add_strategy(restored)
    restored.on_load(saved)
    restored._restore_loaded_state()
    assert list(restored._closes[iid]) == [100.0, 101.0]
    engine.dispose()


def test_interrupted_rebuild_rechecks_broker_and_discards_partial_history(registry):
    registry.register_event("IB-DU1", action(), "operator")
    registry.reconcile("IB-DU1", [identity()], broker_flat=True, now=NOW)
    with pytest.raises(ValueError, match="zero positions"):
        registry.reconcile("IB-DU1", [identity()], broker_flat=False, now=NOW)
    with pytest.raises(ValueError, match="every affected"):
        registry.reconcile("IB-DU1", [], broker_flat=True, now=NOW)
    assert registry.reconcile("IB-DU1", [identity()], broker_flat=True, now=NOW)["rebuild_required"]


def test_interrupted_symbol_replacement_can_resume(registry):
    registry.register_event("IB-DU1", action("SYMBOL_CHANGE", new_con_id=456), "operator")
    registry.reconcile("IB-DU1", [identity(456, "XYZ")], broker_flat=True, now=NOW)
    assert registry.reconcile("IB-DU1", [identity(456, "XYZ")], broker_flat=True, now=NOW)["rebuild_required"]


def test_distinct_conids_cannot_claim_same_symbol(registry):
    with pytest.raises(ValueError, match="duplicate qualified symbol"):
        registry.reconcile("IB-DU1", [identity(456, "XYZ"), identity(789, "XYZ")], broker_flat=True, now=NOW)
    assert len(registry.identities("IB-DU1")) == 1


def test_identity_rebuild_preserves_risk_but_discards_pre_action_models():
    from quant.strategies.ml_strategy import MLStrategy
    from quant.strategies.risk import RiskManager
    old_risk = RiskManager(5000)
    old_risk.engage_kill_switch()
    strategy = SimpleNamespace(
        _loaded_state={"version": 8, "instrument_ids": ["ABC.SMART"], "risk": old_risk.snapshot(),
                       "account_equity_baseline": 5000, "instruments": {"ABC.SMART": {"closes": [100]}}},
        _identity_rebuild_required=True, _identity_generation="new-generation",
        _equity_identities={"ABC.SMART": identity()}, _risk=RiskManager(5000),
        config=SimpleNamespace(instrument_ids=["ABC.SMART"], use_allocated_equity=True),
        cache=SimpleNamespace(positions_open=lambda: [], orders_open=lambda: [], orders_inflight=lambda: []),
        log=SimpleNamespace(warning=lambda message: None),
    )
    MLStrategy._restore_loaded_state(strategy)
    assert not strategy._risk.can_open
    assert strategy._account_equity_baseline == 5000


def test_identity_migration_cannot_discard_state_with_live_exposure():
    from quant.strategies.ml_strategy import MLStrategy
    strategy = SimpleNamespace(_loaded_state={"version": 8, "instrument_ids": ["ABC.SMART"]},
        _identity_rebuild_required=True, _identity_generation="new-generation",
        _equity_identities={"ABC.SMART": identity()},
        config=SimpleNamespace(instrument_ids=["ABC.SMART"]),
        cache=SimpleNamespace(positions_open=lambda: [object()], orders_open=lambda: [], orders_inflight=lambda: []))
    with pytest.raises(ValueError, match="flat account"):
        MLStrategy._restore_loaded_state(strategy)
