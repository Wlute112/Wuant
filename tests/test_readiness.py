import pytest

from quant.run.readiness import (
    LIVE_GATE_CODE,
    LiveCapitalDisabledError,
    assert_live_capital_enabled,
    live_readiness_status,
)


def test_live_capital_gate_is_fail_closed_without_runtime_override():
    status = live_readiness_status()
    assert status["live_capital_enabled"] is False
    assert status["code"] == LIVE_GATE_CODE
    assert status["incomplete"]
    with pytest.raises(LiveCapitalDisabledError, match="Live capital is disabled"):
        assert_live_capital_enabled()
