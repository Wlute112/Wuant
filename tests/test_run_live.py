import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quant.api.schemas import LiveJobRequest
from quant.data.ib_compat import _restore_zerohash_base_quantity
from quant.run.run_live import (
    bar_type_suffix_for_asset,
    instrument_ids_for_asset,
    load_params,
    validate_mode_port,
)


@pytest.mark.parametrize("port", [7497, 4002])
def test_live_rejects_known_paper_ports(port):
    with pytest.raises(ValueError, match="paper port"):
        validate_mode_port(True, port)


@pytest.mark.parametrize("port", [7496, 4001])
def test_paper_rejects_known_live_ports(port):
    with pytest.raises(ValueError, match="live port"):
        validate_mode_port(False, port)


@pytest.mark.parametrize(
    "is_live,port",
    [(False, 7497), (False, 4002), (True, 7496), (True, 4001)],
)
def test_mode_accepts_matching_known_port(is_live, port):
    validate_mode_port(is_live, port)


def test_load_params_requires_json_object(tmp_path):
    path = tmp_path / "params.json"
    path.write_text(json.dumps(["not", "an", "object"]))
    with pytest.raises(ValueError, match="JSON object"):
        load_params(str(path))


def test_load_params_reads_object(tmp_path):
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"entry_threshold": 0.01}))
    assert load_params(str(path)) == ({"entry_threshold": 0.01}, None)


def test_load_params_reads_optimizer_output(tmp_path):
    path = tmp_path / "best_params.json"
    path.write_text(
        json.dumps(
            {
                "params": {"entry_threshold": 0.01, "n_lags": 9},
                "warmup_bars": 180,
                "trials": 40,
                "source_csv": "bars.csv",
            }
        )
    )
    assert load_params(str(path)) == (
        {
            "entry_threshold": 0.01,
            "n_lags": 9,
            "warmup_bars": 180,
        },
        None,
    )


def test_load_params_reads_ibkr_bar_hours(tmp_path):
    path = tmp_path / "best_params.json"
    path.write_text(
        json.dumps(
            {
                "params": {"entry_threshold": 0.01},
                "ibkr_bar_hours": 8,
            }
        )
    )
    params, bar_hours = load_params(str(path))
    assert params == {"entry_threshold": 0.01}
    assert bar_hours == 8


def test_load_params_reads_dashboard_run_artifact(tmp_path):
    path = tmp_path / "optimize_run.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "optimize_deadbeef",
                "kind": "optimize",
                "tickers": ["BTC", "ETH"],
                "best_params": {"entry_threshold": 0.01, "n_lags": 9},
                "trials": [{"number": 0, "state": "COMPLETE"}],
                "ibkr_bar_hours": 4,
            }
        )
    )
    assert load_params(str(path)) == (
        {
            "entry_threshold": 0.01,
            "n_lags": 9,
        },
        4,
    )


def test_load_params_ignores_metadata_and_runtime_owned_fields(tmp_path):
    path = tmp_path / "params.json"
    path.write_text(
        json.dumps(
            {
                "entry_threshold": 0.01,
                "run_id": "not-a-strategy-field",
                "instrument_ids": ["BAD.VALUE"],
                "account_id": "BAD-VALUE",
            }
        )
    )
    assert load_params(str(path)) == ({"entry_threshold": 0.01}, None)


def test_crypto_live_instrument_ids_and_bar_type():
    assert instrument_ids_for_asset(["btc"], "crypto") == ["BTC/USD.ZEROHASH"]
    assert bar_type_suffix_for_asset("crypto") == "-1-DAY-MID-EXTERNAL"


def test_equity_live_instrument_ids_and_bar_type():
    assert instrument_ids_for_asset(["spy", "qqq"], "equity") == [
        "SPY.SMART",
        "QQQ.SMART",
    ]
    assert instrument_ids_for_asset(["spy"], "equity", "arca") == ["SPY.ARCA"]
    assert bar_type_suffix_for_asset("equity") == "-1-DAY-LAST-EXTERNAL"


@pytest.mark.parametrize("bar_hours", [1, 2, 3, 4, 8])
def test_bar_type_suffix_honors_hourly_bar_hours(bar_hours):
    assert bar_type_suffix_for_asset("crypto", bar_hours) == f"-{bar_hours}-HOUR-MID-EXTERNAL"
    assert bar_type_suffix_for_asset("equity", bar_hours) == f"-{bar_hours}-HOUR-LAST-EXTERNAL"


def test_bar_type_suffix_treats_24_hours_as_daily():
    assert bar_type_suffix_for_asset("crypto", 24) == "-1-DAY-MID-EXTERNAL"


def test_bar_type_suffix_rejects_non_native_bar_hours():
    with pytest.raises(ValueError, match="live-subscribable"):
        bar_type_suffix_for_asset("crypto", 12)


@pytest.mark.parametrize("helper", [instrument_ids_for_asset, bar_type_suffix_for_asset])
def test_live_helpers_reject_unknown_asset_class(helper):
    with pytest.raises(ValueError, match="unsupported asset class"):
        if helper is instrument_ids_for_asset:
            helper(["SPY"], "future")
        else:
            helper("future")


def test_live_dashboard_request_accepts_inline_params():
    request = LiveJobRequest(
        tickers=["BTC"],
        port=4001,
        confirm="I UNDERSTAND THIS DEPLOYS REAL CAPITAL",
        params={"entry_threshold": 0.002},
    )
    assert request.params == {"entry_threshold": 0.002}


def test_zerohash_fractional_base_quantity_is_not_converted_to_cash():
    ib_order = SimpleNamespace(cashQty=0, totalQuantity=0)
    order = SimpleNamespace(
        instrument_id=SimpleNamespace(venue="ZEROHASH"),
        is_quote_quantity=False,
        quantity=SimpleNamespace(as_decimal=lambda: Decimal("0.01234567")),
    )

    _restore_zerohash_base_quantity(
        ib_order,
        order,
        SimpleNamespace(is_inverse=True),
    )

    assert ib_order.totalQuantity == Decimal("0.01234567")
    assert ib_order.cashQty > 1e300


def test_zerohash_quote_quantity_stays_cash_quantity():
    ib_order = SimpleNamespace(cashQty=250, totalQuantity=0)
    order = SimpleNamespace(
        instrument_id=SimpleNamespace(venue="ZEROHASH"),
        is_quote_quantity=True,
        quantity=SimpleNamespace(
            as_decimal=lambda: Decimal("250.75"),
            as_double=lambda: 250.75,
        ),
    )

    _restore_zerohash_base_quantity(
        ib_order,
        order,
        SimpleNamespace(is_inverse=True),
    )

    assert ib_order.cashQty == 250.75
    assert ib_order.totalQuantity == 0
