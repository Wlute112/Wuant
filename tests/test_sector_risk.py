from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from quant.api import jobs_routes
from quant.api.schemas import PaperJobRequest, SectorEvidenceRequest
from quant.run.run_live import load_params
from quant.run.sector_risk import sector_snapshot, validate_sector_evidence
from quant.strategies.execution_state import ExecutionSafetyController, OrderRole
from quant.strategies.ml_strategy import MLStrategy


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


def evidence(now=NOW):
    return {"schema_version": 1, "classifications": [
        dict(con_id=1, symbol="AAA", weights={"technology": 1.0},
             source_name="TEST FIXTURE — not market evidence", source_url="https://example.com/fixture",
             as_of=(now - timedelta(days=1)).isoformat(), reviewed_at=now.isoformat(),
             reviewed_by="test fixture", valid_until=(now + timedelta(days=1)).isoformat()),
        dict(con_id=2, symbol="FUND", weights={"technology": 0.5, "financials": 0.5},
             source_name="TEST FIXTURE — not market evidence", source_url="https://example.com/fixture",
             as_of=(now - timedelta(days=1)).isoformat(), reviewed_at=now.isoformat(),
             reviewed_by="test fixture", valid_until=(now + timedelta(days=1)).isoformat()),
    ]}


def info(con_id, symbol, stock_type="COMMON"):
    return {"contract": {"conId": con_id, "symbol": symbol, "primaryExchange": "NASDAQ",
                         "secType": "STK", "currency": "USD"}, "stockType": stock_type}


INFOS = {"AAA.SMART": info(1, "AAA"), "FUND.SMART": info(2, "FUND", "ETF")}


@pytest.mark.parametrize("field,value", [
    ("con_id", True), ("source_url", "http://example.com/source"),
    ("source_url", "https://user:secret@example.com/source"), ("reviewed_by", ""),
    ("weights", {"technology": 0.9}), ("weights", {"unclassified": 1}),
    ("weights", {"technology": float("nan")}), ("weights", {"technology": True}),
    ("as_of", "2026-09-09"), ("as_of", "2026-09-10T00:00:00Z"),
    ("valid_until", "2026-09-09T00:00:00Z"), ("valid_until", "2027-01-01T00:00:00Z"),
])
def test_evidence_rejects_unusable_authority(field, value):
    payload = evidence()
    payload["classifications"][0][field] = value
    with pytest.raises(ValueError):
        validate_sector_evidence(payload, now=NOW)


def test_duplicate_identity_missing_coverage_and_unknown_fields_rejected():
    payload = evidence()
    payload["classifications"].append(deepcopy(payload["classifications"][0]))
    with pytest.raises(ValueError, match="unique"):
        validate_sector_evidence(payload, now=NOW)
    with pytest.raises(ValueError, match="missing: BBB"):
        validate_sector_evidence(evidence(), symbols=["BBB"], now=NOW)
    with pytest.raises(ValueError, match="accepts only"):
        validate_sector_evidence({**evidence(), "override": True}, now=NOW)


def test_gross_stock_and_etf_lookthrough_share_the_sector_limit():
    result = sector_snapshot(evidence(), INFOS, {"AAA.SMART": 2000, "FUND.SMART": 2000},
                             equity=10000, limit=0.30, now=NOW)
    assert result["healthy"]
    assert result["sectors"]["technology"]["notional"] == 3000
    assert result["sectors"]["financials"]["equity_pct"] == 10
    assert len(result["evidence_sha256"]) == 64
    breached = sector_snapshot(evidence(), INFOS, {"AAA.SMART": 2001, "FUND.SMART": 2000},
                               equity=10000, limit=0.30, now=NOW)
    assert breached["status"] == "BREACHED"
    assert not breached["healthy"]


@pytest.mark.parametrize("infos,notionals", [
    ({"AAA.SMART": info(999, "AAA")}, {}),
    ({"AAA.SMART": info(1, "RENAMED")}, {}),
    (INFOS, {"AAA.SMART": float("nan")}),
    (INFOS, {"AAA.SMART": -100}),
    (INFOS, {"UNKNOWN.SMART": 100}),
])
def test_bad_identity_or_unvalued_exposure_fails_closed(infos, notionals):
    result = sector_snapshot(evidence(), infos, notionals, equity=10000, limit=0.3, now=NOW)
    assert result["status"] == "UNAVAILABLE"
    assert not result["healthy"]
    assert result["issues"]


def test_evidence_expiry_blocks_flat_account_and_hash_is_stable():
    first = sector_snapshot(evidence(), INFOS, {}, equity=10000, limit=0.3, now=NOW)
    assert first["healthy"]
    assert first["evidence_sha256"] == sector_snapshot(evidence(), INFOS, {}, equity=10000, limit=0.3, now=NOW)["evidence_sha256"]
    assert not sector_snapshot(evidence(), INFOS, {}, equity=10000, limit=0.3, now=NOW + timedelta(days=1))["healthy"]
    assert not sector_snapshot(None, INFOS, {}, equity=10000, limit=0.3, now=NOW)["healthy"]


def number(value):
    return SimpleNamespace(as_double=lambda: value)


def strategy_fixture():
    positions = [SimpleNamespace(instrument_id="AAA.SMART", quantity=number(10)),
                 SimpleNamespace(instrument_id="FUND.SMART", quantity=number(10), side="SHORT")]
    def order(order_id, iid, qty):
        return SimpleNamespace(client_order_id=order_id, instrument_id=iid, quantity=number(qty),
                               leaves_qty=number(qty), price=number(100), is_quote_quantity=False, is_closed=False)
    entry = order("entry", "AAA.SMART", 5)
    stop = order("stop", "FUND.SMART", 10)
    orders = [entry, stop]
    tick = SimpleNamespace(price=100, ts_event=int(NOW.timestamp() * 1e9))
    obj = SimpleNamespace(
        config=SimpleNamespace(sector_evidence=evidence(), max_sector_exposure_pct=0.3,
                               max_market_data_age_secs=15, asset_class="equity", execution_mode="paper", account_id="IB-DU1"),
        _bar_types={"AAA.SMART": "bars", "FUND.SMART": "bars"},
        _committed_notional={"AAA.SMART": 1500}, _equity=lambda: 10000,
        clock=SimpleNamespace(utc_now=lambda: NOW, timestamp_ns=lambda: int(NOW.timestamp() * 1e9)),
        _execution=SimpleNamespace(orders={"stop": SimpleNamespace(role=OrderRole.STOP_LOSS)}),
        cache=SimpleNamespace(positions_open=lambda: positions, orders_open=lambda: orders,
                              orders_inflight=lambda: [entry], instrument=lambda iid: SimpleNamespace(info=INFOS[iid]),
                              quote_tick=lambda iid: None, trade_tick=lambda iid: tick),
    )
    obj._sector_risk_snapshot = lambda *args: MLStrategy._sector_risk_snapshot(obj, *args)
    return obj, positions, orders


def test_strategy_counts_gross_broker_positions_entries_and_reservations_once():
    obj, positions, orders = strategy_fixture()
    result = obj._sector_risk_snapshot()
    assert result["sectors"]["technology"]["notional"] == 2000  # 1000 stock + 500 entry + 500 ETF
    assert result["sectors"]["financials"]["notional"] == 500
    assert obj._sector_risk_snapshot("AAA.SMART", 1001)["status"] == "BREACHED"
    obj._committed_notional["AAA.SMART"] = 2600  # unacknowledged synchronous reservation
    assert obj._sector_risk_snapshot()["status"] == "BREACHED"
    orders.append(SimpleNamespace(client_order_id="foreign", instrument_id="FUND.SMART", quantity=number(10),
                                  leaves_qty=number(10), price=number(100), is_quote_quantity=False, is_closed=False))
    assert obj._sector_risk_snapshot()["sectors"]["financials"]["notional"] == 1000


def test_stale_marks_and_unclassified_foreign_exposure_cannot_appear_safe():
    obj, positions, _ = strategy_fixture()
    obj.cache.trade_tick = lambda iid: SimpleNamespace(price=100, ts_event=int((NOW - timedelta(seconds=16)).timestamp() * 1e9))
    assert not obj._sector_risk_snapshot()["healthy"]
    obj, positions, _ = strategy_fixture()
    positions.append(SimpleNamespace(instrument_id="FOREIGN.SMART", quantity=number(1)))
    assert not obj._sector_risk_snapshot()["healthy"]


def test_final_submission_boundary_rejects_sector_breach(monkeypatch):
    obj, *_ = strategy_fixture()
    obj._execution_safety = ExecutionSafetyController()
    obj._last_mark = {}
    audits = []
    obj._audit_event = lambda *args, **kwargs: audits.append(args)
    monkeypatch.setattr("quant.strategies.ml_strategy.broker_connectivity.snapshot", lambda *args: {"healthy": True})
    order = SimpleNamespace(instrument_id="AAA.SMART", price=number(100), quantity=number(11))
    assert MLStrategy._register_and_submit_order(obj, order, role=OrderRole.ENTRY) is False
    assert audits[0][0] == "SECTOR_PREFLIGHT_REJECTED"


def test_proposed_limit_entry_is_valued_at_current_gross_mark():
    obj, *_ = strategy_fixture()
    # Existing technology exposure is $2,000. A bid for 11 shares at $90
    # cannot pass the $3,000 limit when those shares currently mark at $100.
    assert obj._sector_risk_snapshot("AAA.SMART", 990, 11)["status"] == "BREACHED"
    assert obj._sector_risk_snapshot("AAA.SMART", 900, 10)["healthy"]


def test_schema_version_cannot_be_a_boolean():
    with pytest.raises(ValueError, match="schema_version"):
        validate_sector_evidence({**evidence(), "schema_version": True}, now=NOW)


def test_dashboard_validation_and_execution_upload_reach_runtime(tmp_path, monkeypatch):
    payload = evidence(datetime.now(timezone.utc) - timedelta(seconds=1))
    response = jobs_routes.validate_sector_upload(SectorEvidenceRequest(evidence=payload, tickers=["AAA", "FUND"]))
    assert response["status"] == "VALIDATED"
    with pytest.raises(HTTPException) as exc:
        jobs_routes.validate_sector_upload(SectorEvidenceRequest(evidence=payload, tickers=["UNKNOWN"]))
    assert exc.value.status_code == 400
    monkeypatch.setattr(jobs_routes, "JOBS_DIR", tmp_path)
    req = PaperJobRequest(asset_class="equity", tickers=["AAA", "FUND"], sector_evidence=payload,
                          params={"params": {"sector_evidence": {"bad": True}, "n_lags": 7}, "ibkr_bar_hours": 4})
    path = jobs_routes._execution_params_file("paper-test", req)
    params, hours = load_params(path)
    assert hours == 4 and params["n_lags"] == 7
    assert params["sector_evidence"] == payload
    persisted = json.loads(open(path).read())
    assert persisted["sector_evidence"] == payload
