"""Read-only asset operating profiles for the dashboard switcher."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from quant.run.asset_profiles import ASSET_PROFILES, get_asset_profile

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.get("")
def list_profiles():
    return [get_asset_profile(name) for name in ASSET_PROFILES]


@router.get("/{asset_class}")
def get_profile(asset_class: str):
    try:
        return get_asset_profile(asset_class)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
