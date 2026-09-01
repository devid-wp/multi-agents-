"""routers/changes.py — approval диффов (вынесено из main.py 0.6.0)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.changes import ChangeStore
from core.config import settings

router = APIRouter(prefix="/api/changes", tags=["changes"])


class ChangeDecisionPayload(BaseModel):
    approve: bool


@router.get("")
async def list_changes():
    return {"changes": ChangeStore(settings.workspace_dir).list()}


@router.post("/{proposal_id}/decision")
async def decide_change(proposal_id: str, payload: ChangeDecisionPayload):
    try:
        return ChangeStore(settings.workspace_dir).decide(proposal_id, payload.approve)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
