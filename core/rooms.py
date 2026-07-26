from __future__ import annotations

import json
import os
import re
from pathlib import Path
from threading import Lock
from typing import Any

ROOM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
DEFAULT_ROOMS = [
    {"id": "general", "name": "General", "strategy": "auto", "builtin": True},
]


class RoomStore:
    """Small atomic JSON store for local-alpha chat rooms."""

    def __init__(self, workspace_dir: str):
        self.path = Path(workspace_dir) / ".trinity" / "rooms.json"
        self._lock = Lock()

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return [dict(room) for room in DEFAULT_ROOMS]
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else [dict(room) for room in DEFAULT_ROOMS]
        except (OSError, json.JSONDecodeError):
            return [dict(room) for room in DEFAULT_ROOMS]

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._load()

    def create(self, room_id: str, name: str, strategy: str = "auto") -> dict[str, Any]:
        room_id = room_id.strip().lower()
        name = name.strip()
        if not ROOM_ID_RE.fullmatch(room_id):
            raise ValueError("room id must contain 1-40 lowercase letters, digits, '_' or '-'")
        if not name or len(name) > 80:
            raise ValueError("room name must contain 1-80 characters")
        with self._lock:
            rooms = self._load()
            if any(room["id"] == room_id for room in rooms):
                raise ValueError("room already exists")
            room = {"id": room_id, "name": name, "strategy": strategy, "builtin": False}
            rooms.append(room)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(rooms, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.path)
            return room

    def exists(self, room_id: str) -> bool:
        return any(room["id"] == room_id for room in self.list())

    @staticmethod
    def session_id(client_id: str, room_id: str) -> str:
        safe_client = re.sub(r"[^a-zA-Z0-9_-]", "", client_id)[:80] or "anonymous"
        return f"{safe_client}--room-{room_id}"
