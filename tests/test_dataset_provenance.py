import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import pytest

from quant.data.provenance import archive, describe, pin_csv
from quant.data.research_preflight import inspect_csv
from quant.data.ibkr_fetch import _atomic_write_bars


def frame(ticker="OLD", close=101):
    return pd.DataFrame([dict(timestamp="2025-01-02T14:30:00Z", ticker=ticker,
                              open=100, high=105, low=95, close=close, volume=100,
                              source="fixture", retrieved_at="2026-09-21T00:00:00Z",
                              con_id="123", membership_start="2024-01-01",
                              membership_end="2025-06-01", symbol_alias="OLDER")])


def test_inspection_is_read_only_and_does_not_invent_historical_evidence(tmp_path):
    path = tmp_path / "bars.csv"
    frame().drop(columns=["retrieved_at", "con_id", "membership_start", "membership_end"]).to_csv(path, index=False)
    report = inspect_csv(path, ["OLD"], "equity")
    evidence = report["provenance"]
    assert evidence["status"] == "unarchived"
    assert evidence["observed_at"] is None
    assert evidence["universe"][0]["declarations"][0]["membership_start"] is None
    assert list(tmp_path.iterdir()) == [path]
    json.dumps(report, allow_nan=False)


def test_revisions_preserve_removed_symbols_and_exact_input_bytes(tmp_path):
    path = tmp_path / "bars.csv"
    original = pd.concat([frame(), frame("KEEP")], ignore_index=True)
    _atomic_write_bars(path, original)
    first = describe(path)
    pinned = pin_csv(path)
    replacement = pd.concat([frame("KEEP", 102), frame("NEW")], ignore_index=True)
    _atomic_write_bars(path, replacement)
    second = describe(path)
    assert second["changes"] == dict(added_symbols=["NEW"], removed_symbols=["OLD"],
                                      added_bars=1, removed_bars=1, revised_bars=1)
    assert second["parent_sha256"] == first["sha256"]
    assert second["history"][0]["sha256"] == first["sha256"]
    assert pd.read_csv(pinned).ticker.tolist() == ["OLD", "KEEP"]
    assert describe(pinned)["sha256"] == first["sha256"]
    assert second["universe"][0]["declarations"][0]["membership_end"] == "2025-06-01"


def test_retrieval_time_alone_is_not_a_market_revision(tmp_path):
    path = tmp_path / "bars.csv"
    _atomic_write_bars(path, frame())
    revised = frame()
    revised["retrieved_at"] = "2026-09-22T00:00:00Z"
    _atomic_write_bars(path, revised)
    assert describe(path)["changes"]["revised_bars"] == 0


@pytest.mark.parametrize("target", ["bytes", "manifest", "parent"])
def test_corrupt_evidence_fails_closed(tmp_path, target):
    path = tmp_path / "bars.csv"
    _atomic_write_bars(path, frame())
    first = describe(path)
    _atomic_write_bars(path, frame(close=102))
    current = describe(path)
    selected = first if target == "parent" else current
    snapshot = Path(selected["snapshot_csv"])
    if target == "manifest":
        snapshot.with_suffix(".json").write_text('{}')
    else:
        snapshot.write_text('corrupted')
    assert inspect_csv(path, ["OLD"], "equity")["execution_eligible"] is False
    with pytest.raises(ValueError):
        pin_csv(path)
    if target == "bytes":
        with pytest.raises(ValueError):
            pin_csv(snapshot)


def test_parallel_retries_reuse_one_verified_snapshot(tmp_path):
    path = tmp_path / "bars.csv"
    frame().to_csv(path, index=False)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: archive(path), range(8)))
    assert len({result["manifest_sha256"] for result in results}) == 1
    assert len(list(path.with_name("bars.csv.provenance").glob("*.csv"))) == 1


def test_invalid_old_csv_can_be_preserved_before_repair(tmp_path):
    path = tmp_path / "bars.csv"
    path.write_bytes(b"broken source data\n")
    _atomic_write_bars(path, frame())
    evidence = describe(path)
    assert evidence["changes"]["revised_bars"] is None
    assert evidence["history"]


def test_snapshot_reuse_does_not_change_its_parent(tmp_path):
    path = tmp_path / "bars.csv"
    _atomic_write_bars(path, frame())
    initial = describe(path)
    _atomic_write_bars(path, frame(close=102))
    _atomic_write_bars(path, frame())
    assert describe(path)["manifest_sha256"] == initial["manifest_sha256"]
    assert len(describe(path)["history"]) == 2
    assert describe(path)["changes"]["revised_bars"] == 1


def test_replaying_old_snapshot_does_not_rewind_source_history(tmp_path):
    path = tmp_path / "bars.csv"
    _atomic_write_bars(path, frame())
    old = pin_csv(path)
    _atomic_write_bars(path, frame(close=102))
    latest = describe(path)
    pin_csv(old)
    assert describe(path)["history"] == latest["history"]
    _atomic_write_bars(path, frame(close=103))
    assert describe(path)["parent_sha256"] == latest["sha256"]


def test_missing_manifest_cannot_be_reconstructed_for_snapshot(tmp_path):
    path = tmp_path / "bars.csv"
    frame().to_csv(path, index=False)
    pinned = Path(pin_csv(path))
    pinned.with_suffix(".json").unlink()
    assert describe(pinned)["status"] == "integrity_error"
    with pytest.raises(ValueError, match="missing its provenance"):
        pin_csv(pinned)


def test_contract_requires_the_original_manifest_but_supports_legacy_hashes(tmp_path):
    from quant.data.provenance import verify_contract_dataset
    path = tmp_path / "bars.csv"
    frame().to_csv(path, index=False)
    record = archive(path)
    contract = dict(source_csv=record["snapshot_csv"], source_csv_sha256=record["sha256"],
                    dataset_manifest_sha256=record["manifest_sha256"])
    verify_contract_dataset(contract)
    with pytest.raises(ValueError, match="Locked dataset provenance"):
        verify_contract_dataset({**contract, "dataset_manifest_sha256": "0" * 64})
    verify_contract_dataset({key: value for key, value in contract.items() if key != "dataset_manifest_sha256"})


def test_new_equity_campaign_visible_before_first_seed_finishes(tmp_path, monkeypatch):
    from quant.api import campaigns
    monkeypatch.setattr(campaigns, "CAMPAIGNS_DIR", tmp_path)
    path = tmp_path / "new.json"
    path.write_text(json.dumps(dict(schema_version=1, campaign_id="new", optimizer_args=["--asset-class=equity"], seeds=[1, 2, 3])))
    assert campaigns.list_campaigns()[0]["asset_class"] == "equity"


def test_backtest_uses_pinned_data_and_saves_provenance_when_source_changes(tmp_path):
    # Nautilus owns process-global native logging; production research also runs
    # in a fresh worker, so do not reinitialize it after unrelated engine tests.
    import subprocess
    import sys
    script = r"""
import json
import sys
from pathlib import Path
from quant.data.generate_sample_bars import generate
from quant.run import run_backtest, artifacts
root = Path(sys.argv[1])
path = root / "sample.csv"
generate(["BTC"], n_days=180, seed=42).to_csv(path, index=False)
original = path.read_bytes()
artifacts.RUNS_DIR = root / "runs"
original_build = run_backtest.build_and_run
def replace_source_then_build(**kwargs):
    path.write_bytes(b"source replaced while research was running\n")
    assert Path(kwargs["csv_path"]).read_bytes() == original
    return original_build(**kwargs)
run_backtest.build_and_run = replace_source_then_build
sys.argv = ["backtest", "--csv", str(path), "--tickers", "BTC", "--no-news",
            "--run-id", "provenance_smoke", "--log-level", "ERROR"]
run_backtest.main()
result = json.loads((root / "runs" / "provenance_smoke.json").read_text())
assert result["dataset_provenance"]["status"] == "archived"
assert Path(result["source_csv"]).read_bytes() == original
assert result["dataset_provenance"]["universe"][0]["ticker"] == "BTC"
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_source_observation_journal_is_not_silently_rebuilt(tmp_path):
    path = tmp_path / "bars.csv"
    frame().to_csv(path, index=False)
    pinned = Path(pin_csv(path))
    (pinned.parent / "observations.json").unlink()
    assert describe(path)["status"] == "integrity_error"
    with pytest.raises(ValueError, match="journal is missing"):
        archive(path)
    # Retained bytes and their immutable manifest remain replayable.
    assert describe(pinned)["status"] == "archived"


def test_all_campaign_seeds_share_snapshot_even_when_source_replaced(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from quant.optimize import multi_seed
    path = tmp_path / "bars.csv"
    frame().to_csv(path, index=False)
    original = path.read_bytes()
    manifest = tmp_path / "campaign.json"
    studies, inputs = {}, []
    def load_study(*, study_name, storage):
        if study_name not in studies:
            raise KeyError(study_name)
        return studies[study_name]
    def run(command, check):
        # argparse uses the final --csv override managed by multi_seed.
        csv = command[max(i for i, value in enumerate(command) if value == "--csv") + 1]
        assert Path(csv).read_bytes() == original
        inputs.append(csv)
        path.write_bytes(b"replacement between seed studies\n")
        name = command[command.index("--run-id") + 1]
        studies[name] = SimpleNamespace(user_attrs={"validation_contract": {"source_csv": csv}}, trials=[None] * 100,
                                       get_trials=lambda **kwargs: [])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(multi_seed.optuna, "load_study", load_study)
    monkeypatch.setattr(multi_seed, "subprocess", SimpleNamespace(run=run))
    monkeypatch.setattr(sys, "argv", ["multi_seed", "--campaign-id", "fixture", "--manifest", str(manifest),
                                      "--seeds", "1", "2", "3", "--trials", "100", "--csv", str(path)])
    multi_seed.main()
    assert len(inputs) == 3 and len(set(inputs)) == 1
    assert json.loads(manifest.read_text())["dataset_provenance"]["snapshot_csv"] == inputs[0]

    # Retrying the completed campaign keeps its original bytes even though the
    # caller still names the now-replaced mutable source.
    multi_seed.main()
    assert len(inputs) == 3
