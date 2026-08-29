from quant.strategies.risk import RiskConfig, RiskManager


def test_pretrade_controls_report_every_hard_limit_and_price_collar():
    manager = RiskManager(
        10_000,
        RiskConfig(
            max_order_notional_pct=0.10,
            max_symbol_exposure_pct=0.20,
            max_sector_exposure_pct=0.30,
            max_gross_exposure_pct=0.80,
            max_concentration_pct=0.50,
            price_collar_pct=0.05,
        ),
    )

    assert manager.pretrade_violations(
        equity=10_000,
        order_notional=1_100,
        symbol_exposure_after=4_500,
        sector_exposure_after=3_100,
        gross_exposure_after=8_500,
        order_price=106,
        reference_price=100,
    ) == [
        "MAX_ORDER_NOTIONAL",
        "MAX_SYMBOL_EXPOSURE",
        "MAX_GROSS_EXPOSURE",
        "MAX_CONCENTRATION",
        "MAX_SECTOR_EXPOSURE",
        "PRICE_COLLAR",
    ]


def test_pretrade_controls_fail_closed_without_equity_or_valid_price():
    manager = RiskManager(5_000)

    assert manager.pretrade_violations(
        equity=0,
        order_notional=1,
        symbol_exposure_after=1,
        gross_exposure_after=1,
    ) == ["ACCOUNT_EQUITY_UNAVAILABLE"]
    assert "INVALID_PRICE" in manager.pretrade_violations(
        equity=5_000,
        order_notional=1,
        symbol_exposure_after=1,
        gross_exposure_after=1,
        order_price=0,
        reference_price=100,
    )
