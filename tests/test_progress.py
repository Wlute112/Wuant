import json

from quant.run.progress import ProgressWriter


def test_progress_writer_merges_atomic_snapshots(tmp_path):
    path = tmp_path / "job_progress.json"
    writer = ProgressWriter(str(path), kind="backtest", percent=0)

    writer.update(phase="replay", percent=35, bars_processed=350)
    writer.update(percent=50, equity=5125.25)

    snapshot = json.loads(path.read_text())
    assert snapshot["kind"] == "backtest"
    assert snapshot["phase"] == "replay"
    assert snapshot["percent"] == 50
    assert snapshot["bars_processed"] == 350
    assert snapshot["equity"] == 5125.25
    assert snapshot["updated_at"]
    assert not path.with_suffix(".json.tmp").exists()


def test_equity_dashboard_replay_keeps_books_with_bars_and_counts_observed_bars(tmp_path, monkeypatch):
    from functools import partial
    import pandas as pd
    from quant.run import run_backtest

    # Nautilus logging is process-global; other tests initialize its Rust
    # logger. Match the engine suite's bypass to avoid double initialization.
    monkeypatch.setattr(run_backtest, "build_engine", partial(run_backtest.build_engine, bypass_logging=True))

    dates = pd.date_range("2026-01-02", periods=4, freq="B", tz="UTC")
    csv = tmp_path / "equity.csv"
    pd.DataFrame([
        dict(timestamp=date, ticker=ticker, open=100, high=101, low=99, close=100, volume=10000)
        for date in dates for ticker in ("SPY", "QQQ")
    ]).to_csv(csv, index=False)
    path = tmp_path / "progress.json"
    engine = run_backtest._run_with_progress(
        csv_path=str(csv), tickers=["SPY", "QQQ"], overrides={}, starting_cash=5000,
        log_level="ERROR", asset_class="equity", progress=ProgressWriter(str(path)),
    )
    try:
        snapshot = json.loads(path.read_text())
        assert snapshot["bars_processed"] == snapshot["bars_total"] == 8
        assert snapshot["percent"] == 100
        assert snapshot["equity"] == 5000
    finally:
        engine.dispose()
