"""Read-only research-campaign reports for the optimization hub."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from quant.api.jobs import WORKDIR

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])
CAMPAIGNS_DIR = WORKDIR / "quant" / "optimize" / "campaigns"


def _read(path: Path) -> dict | None:
    try:
        with path.open() as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _paths(campaign_id: str) -> dict[str, Path]:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not campaign_id or any(char not in allowed for char in campaign_id):
        raise HTTPException(400, "Invalid campaign id.")
    return {
        "manifest": CAMPAIGNS_DIR / f"{campaign_id}.json",
        "comparison": CAMPAIGNS_DIR / f"{campaign_id}_comparison.json",
        "robustness": CAMPAIGNS_DIR / f"{campaign_id}_robustness.json",
        "promoted": CAMPAIGNS_DIR / f"{campaign_id}_promoted_params.json",
    }


def _summary(path: Path, manifest: dict) -> dict:
    campaign_id = str(manifest.get("campaign_id") or path.stem)
    paths = _paths(campaign_id)
    studies = manifest.get("studies") or []
    return {
        "campaign_id": campaign_id,
        "updated_at": manifest.get("updated_at"),
        "asset_class": (manifest.get("validation_contract") or {}).get("asset_class"),
        "seeds": manifest.get("seeds") or [],
        "trials_per_seed": manifest.get("trials_per_seed"),
        "studies_complete": sum(item.get("status") == "COMPLETE" for item in studies),
        "studies_total": len(manifest.get("seeds") or studies),
        "comparison_ready": paths["comparison"].is_file(),
        "robustness_ready": paths["robustness"].is_file(),
        "promotion_status": (manifest.get("outer_holdout") or {}).get("status", "UNTOUCHED"),
    }


@router.get("")
def list_campaigns():
    if not CAMPAIGNS_DIR.is_dir():
        return []
    result = []
    for path in CAMPAIGNS_DIR.glob("*.json"):
        if path.stem.endswith(("_comparison", "_robustness", "_promoted_params")):
            continue
        manifest = _read(path)
        if manifest and manifest.get("schema_version"):
            result.append(_summary(path, manifest))
    return sorted(result, key=lambda item: item.get("updated_at") or 0, reverse=True)


@router.get("/{campaign_id}")
def get_campaign(campaign_id: str):
    paths = _paths(campaign_id)
    manifest = _read(paths["manifest"])
    if manifest is None:
        raise HTTPException(404, f"Campaign {campaign_id!r} was not found.")
    return {
        **_summary(paths["manifest"], manifest),
        "manifest": manifest,
        "comparison": _read(paths["comparison"]),
        "robustness": _read(paths["robustness"]),
        "promoted_params": _read(paths["promoted"]),
    }
