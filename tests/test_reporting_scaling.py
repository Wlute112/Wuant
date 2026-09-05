import json

from quant.api.jobs import _tail_lines
from quant.run import artifacts


def test_log_tail_handles_large_files_unicode_and_unterminated_lines(tmp_path):
    path = tmp_path / "job.log"
    lines = [f"line {i} café π" for i in range(20000)]
    path.write_text("\n".join(lines))
    assert _tail_lines(path, 200) == lines[-200:]
    assert _tail_lines(path, 0) == []
    with path.open("a") as stream:
        stream.write("\n")
    assert _tail_lines(path, 1) == lines[-1:]


def test_run_summaries_reuse_small_cache_and_refresh_replaced_files(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "RUNS_DIR", tmp_path)
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"metrics": {"pnl": 1}, "equity_curve": list(range(10000))}))
    artifacts._run_summary.cache_clear()
    first = artifacts.list_run_summaries()
    first[0]["metrics"]["pnl"] = 999
    assert artifacts.list_run_summaries()[0]["metrics"]["pnl"] == 1
    assert artifacts._run_summary.cache_info().hits == 1
    replacement = tmp_path / "replacement.tmp"
    replacement.write_text(json.dumps({"metrics": {"pnl": 2}}))
    replacement.replace(path)
    assert artifacts.list_run_summaries()[0]["metrics"]["pnl"] == 2
    path.unlink()
    assert artifacts.list_run_summaries() == []
