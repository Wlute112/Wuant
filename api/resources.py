"""Host research queue and authenticated budget configuration."""
from fastapi import APIRouter, Depends, HTTPException
import sqlite3
import subprocess
from pydantic import BaseModel, ConfigDict, Field

from quant.api.operations import authenticate
from quant.run.admission import AdmissionStore

router = APIRouter(prefix="/api/research-resources", tags=["research"])


class ResourceLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu_slots: int = Field(ge=1, strict=True)
    memory_gb: float = Field(gt=0, allow_inf_nan=False)


@router.get("")
def status():
    try:
        return AdmissionStore().status()
    except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        raise HTTPException(503, "Research resource registry unavailable") from exc


@router.put("")
def configure(req: ResourceLimits, operator=Depends(authenticate)):
    try:
        return AdmissionStore().configure(req.cpu_slots, req.memory_gb, operator)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        raise HTTPException(503, "Research resource registry unavailable") from exc
