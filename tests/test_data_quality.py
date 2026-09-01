from datetime import datetime, timedelta, timezone

from quant.data.quality import BarQualityGate, verify_freshness


def _bar(gate, timestamp_ns, **overrides):
    values = {"open_price": 100, "high_price": 102, "low_price": 99, "close_price": 101, "volume": 10}
    values.update(overrides)
    return gate.validate("QQQ.SMART", timestamp_ns=timestamp_ns, **values)


def test_exact_duplicate_is_idempotent_but_revision_fails_closed():
    gate = BarQualityGate(expected_interval_seconds=3600, continuous_market=False)
    assert _bar(gate, 1_000_000_000).accepted
    duplicate = _bar(gate, 1_000_000_000)
    assert not duplicate.accepted
    assert duplicate.issues[0].code == "DUPLICATE"
    revised = _bar(gate, 1_000_000_000, close_price=100.5)
    assert revised.critical
    assert revised.issues[0].code == "REVISED_BAR"


def test_malformed_ohlc_and_continuous_market_gap_are_critical():
    gate = BarQualityGate(expected_interval_seconds=60, continuous_market=True)
    malformed = _bar(gate, 1_000_000_000, high_price=98)
    assert malformed.critical
    assert _bar(gate, 1_000_000_000).accepted
    gap = _bar(gate, 500_000_000_000)
    assert gap.critical
    assert any(issue.code == "DATA_GAP" for issue in gap.issues)


def test_session_market_gap_warns_without_discarding_bar():
    gate = BarQualityGate(expected_interval_seconds=3600, continuous_market=False)
    assert _bar(gate, 1_000_000_000).accepted
    report = _bar(gate, 100_000_000_000_000)
    assert report.accepted
    assert any(issue.severity == "WARNING" for issue in report.issues)


def test_freshness_checks_missing_stale_and_future_data_only_during_open_session():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = {
        "FRESH": int((now - timedelta(seconds=2)).timestamp() * 1e9),
        "STALE": int((now - timedelta(seconds=30)).timestamp() * 1e9),
        "FUTURE": int((now + timedelta(seconds=30)).timestamp() * 1e9),
    }
    report = verify_freshness(
        ["FRESH", "STALE", "FUTURE", "MISSING", "CLOSED"],
        timestamps,
        max_age_seconds=10,
        now=now,
        session_open={"CLOSED": False},
    )
    assert not report.passed
    assert report.stale_instruments == ("STALE",)
    assert report.missing_instruments == ("MISSING",)
    assert report.clock_skew_instruments == ("FUTURE",)
