"""routers/rooms.py — rooms CRUD (вынесено из main.py 0.6.0)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.config import settings
from core.rooms import RoomStore

router = APIRouter(prefix="/api/rooms", tags=["rooms"])


class RoomCreatePayload(BaseModel):
    id: str
    name: str
    strategy: str = "auto"


class RoomRenamePayload(BaseModel):
    name: str


def _store() -> RoomStore:
    return RoomStore(settings.workspace_dir)


@router.get("")
async def list_rooms():
    return {"rooms": _store().list()}


@router.post("", status_code=201)
async def create_room(payload: RoomCreatePayload):
    try:
        room = _store().create(payload.id, payload.name, payload.strategy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return room


@router.put("/{room_id}")
async def rename_room(room_id: str, payload: RoomRenamePayload):
    try:
        return _store().rename(room_id, payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{room_id}")
async def delete_room(room_id: str):
    try:
        _store().delete(room_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}
