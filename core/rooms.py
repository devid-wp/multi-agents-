from __future__ import annotations

import json
import re
from pathlib import Path
import sqlite3
from threading import Lock  # sync store; см. комментарий в core/changes.py
from typing import Any
from core.db import db_path, init_db

ROOM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
DEFAULT_ROOMS = [
    {"id": "general", "name": "General", "strategy": "auto", "builtin": True},
]


class RoomStore:
    """SQLite-only store for chat rooms (JSON removed in 0.6.0)."""

    def __init__(self, workspace_dir: str):
        self.path = Path(workspace_dir) / ".trinity" / "rooms.json"
        self._lock = Lock()

    def _load(self) -> list[dict[str, Any]]:
        ws = str(self.path.parent.parent)
        try:
            init_db(ws)
            con = sqlite3.connect(str(db_path(ws)))
            cur = con.execute("SELECT data FROM rooms ORDER BY builtin DESC, id")
            rows = cur.fetchall()
            con.close()
            if rows:
                return [json.loads(r[0]) for r in rows]
            return [dict(room) for room in DEFAULT_ROOMS]
        except Exception:
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
            ws = str(self.path.parent.parent)
            init_db(ws)
            con = sqlite3.connect(str(db_path(ws)))
            cur = con.execute("SELECT 1 FROM rooms WHERE id=?", (room_id,))
            if cur.fetchone():
                con.close()
                raise ValueError("room already exists")
            room = {"id": room_id, "name": name, "strategy": strategy, "builtin": False}
            con.execute("INSERT INTO rooms (id, name, strategy, builtin, data) VALUES (?,?,?,?,?)",
                        (room_id, name, strategy, 0, json.dumps(room, ensure_ascii=False)))
            con.commit()
            con.close()
            return room

    def rename(self, room_id: str, new_name: str) -> dict[str, Any]:
        new_name = new_name.strip()
        if not new_name or len(new_name) > 80:
            raise ValueError("room name must contain 1-80 characters")
        with self._lock:
            ws = str(self.path.parent.parent)
            init_db(ws)
            con = sqlite3.connect(str(db_path(ws)))
            cur = con.execute("SELECT data FROM rooms WHERE id=?", (room_id,))
            row = cur.fetchone()
            if not row:
                con.close()
                raise ValueError("room not found")
            data = json.loads(row[0])
            if data.get("builtin"):
                con.close()
                raise ValueError("cannot rename builtin room")
            data["name"] = new_name
            con.execute("UPDATE rooms SET name=?, data=? WHERE id=?", (new_name, json.dumps(data, ensure_ascii=False), room_id))
            con.commit()
            con.close()
            return data

    def delete(self, room_id: str) -> None:
        with self._lock:
            ws = str(self.path.parent.parent)
            init_db(ws)
            con = sqlite3.connect(str(db_path(ws)))
            cur = con.execute("SELECT data FROM rooms WHERE id=?", (room_id,))
            row = cur.fetchone()
            if not row:
                con.close()
                raise ValueError("room not found")
            data = json.loads(row[0])
            if data.get("builtin"):
                con.close()
                raise ValueError("cannot delete builtin room")
            con.execute("DELETE FROM rooms WHERE id=?", (room_id,))
            con.commit()
            con.close()

    def exists(self, room_id: str) -> bool:
        return any(room["id"] == room_id for room in self.list())

    @staticmethod
    def session_id(client_id: str, room_id: str) -> str:
        safe_client = re.sub(r"[^a-zA-Z0-9_-]", "", client_id)[:80] or "anonymous"
        return f"{safe_client}--room-{room_id}"
