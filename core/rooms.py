from __future__ import annotations

import json
import os
import re
from pathlib import Path
import sqlite3
import json
from threading import Lock  # sync store; см. комментарий в core/changes.py
from typing import Any
try:
    from core.db import db_path, init_db, is_enabled
except ImportError:
    def is_enabled(ws=None): return False  # type: ignore
    def init_db(ws=None): return None  # type: ignore

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
        if is_enabled(str(self.path.parent.parent)):
            try:
                ws = str(self.path.parent.parent)
                init_db(ws)
                con = sqlite3.connect(str(db_path(ws)))
                cur = con.execute("SELECT data FROM rooms ORDER BY builtin DESC, id")
                rows = cur.fetchall()
                con.close()
                if rows:
                    return [json.loads(r[0]) for r in rows]
                return [dict(room) for room in DEFAULT_ROOMS]
            except Exception:
                pass
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
            if is_enabled(str(self.path.parent.parent)):
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

    def rename(self, room_id: str, new_name: str) -> dict[str, Any]:
        new_name = new_name.strip()
        if not new_name or len(new_name) > 80:
            raise ValueError("room name must contain 1-80 characters")
        with self._lock:
            if is_enabled(str(self.path.parent.parent)):
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
            rooms = self._load()
            for r in rooms:
                if r["id"] == room_id:
                    if r.get("builtin"):
                        raise ValueError("cannot rename builtin room")
                    r["name"] = new_name
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    temp = self.path.with_suffix(".tmp")
                    temp.write_text(json.dumps(rooms, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(temp, self.path)
                    return r
            raise ValueError("room not found")

    def delete(self, room_id: str) -> None:
        with self._lock:
            if is_enabled(str(self.path.parent.parent)):
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
                return
            rooms = self._load()
            filtered = [r for r in rooms if r["id"] != room_id]
            if len(filtered) == len(rooms):
                raise ValueError("room not found")
            target = next((r for r in rooms if r["id"] == room_id), None)
            if target and target.get("builtin"):
                raise ValueError("cannot delete builtin room")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(filtered, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.path)

    def exists(self, room_id: str) -> bool:
        return any(room["id"] == room_id for room in self.list())

    @staticmethod
    def session_id(client_id: str, room_id: str) -> str:
        safe_client = re.sub(r"[^a-zA-Z0-9_-]", "", client_id)[:80] or "anonymous"
        return f"{safe_client}--room-{room_id}"
