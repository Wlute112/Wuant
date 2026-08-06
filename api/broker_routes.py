"""Dashboard endpoints for the read-only IBKR connection monitor."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from quant.api.broker_monitor import BrokerMonitor

router = APIRouter(prefix="/api/broker", tags=["broker"])
monitor = BrokerMonitor()


class BrokerConfigRequest(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=7497, ge=1, le=65535)
    account_id: str = ""
    mode: str = "paper"


@router.get("/status")
def broker_status():
    return monitor.status()


@router.post("/config")
def configure_broker(request: BrokerConfigRequest):
    return monitor.configure(request.model_dump())
