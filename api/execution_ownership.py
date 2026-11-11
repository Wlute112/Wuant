"""Read-only local broker-writer ownership; no takeover/unlock endpoint."""
from fastapi import APIRouter, HTTPException

from quant.run.execution_ownership import ownership_status

router = APIRouter(prefix="/api/execution-ownership", tags=["execution"])


@router.get("")
def status():
    try:
        return ownership_status()
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "Exclusive execution ownership cannot be verified") from exc
