"""Dashboard endpoints for the read-only IBKR connection monitor."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from quant.api.broker_monitor import BrokerMonitor

router = APIRouter(prefix="/api/broker", tags=["broker"])
monitor = BrokerMonitor()


class BrokerConfigRequest(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=7497, ge=1, le=65535)
    account_id: str = ""
    mode: str = "paper"


class LiveBarsRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=15)
    asset_class: Literal["crypto", "equity"] = "equity"
    bar_hours: Literal[1, 2, 3, 4, 8, 24] = 1
    include_extended_hours: bool = False


@router.get("/status")
def broker_status():
    return monitor.status()


@router.post("/config")
def configure_broker(request: BrokerConfigRequest):
    return monitor.configure(request.model_dump())


@router.post("/bars/subscribe")
def subscribe_bars(request: LiveBarsRequest):
    try:
        return monitor.subscribe_bars(**request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/bars")
def get_bars(
    symbol: str = Query(min_length=1, max_length=15),
    asset_class: Literal["crypto", "equity"] = "equity",
    bar_hours: int = Query(default=1, ge=1, le=24),
    include_extended_hours: bool = False,
):
    try:
        return monitor.bar_snapshot(
            symbol,
            asset_class=asset_class,
            bar_hours=bar_hours,
            include_extended_hours=include_extended_hours,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
