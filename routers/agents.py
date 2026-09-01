"""routers/agents.py — active agent switch (вынесено из main.py 0.6.0)."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from core.models import AgentName

router = APIRouter(prefix="/api/agents", tags=["agents"])

ACTIVE_AGENT: AgentName = AgentName.PLANNER


class AgentSwitchPayload(BaseModel):
    agent: AgentName


@router.get("/active")
async def get_active_agent():
    return {"agent": ACTIVE_AGENT}


@router.post("/switch")
async def switch_agent(payload: AgentSwitchPayload):
    global ACTIVE_AGENT
    ACTIVE_AGENT = payload.agent
    return {"agent": ACTIVE_AGENT}
