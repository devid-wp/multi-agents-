"""
core/db.py — SQLite backend для Фазы 2.

Реализует лёгкий sqlite слой для history/rooms/changes.
С 0.5.0 включён по умолчанию (settings.use_sqlite=True) — JSON остаётся fallback.
Таблицы создаются в .trinity/trinity.db, данные мигрируют из JSON один раз.

Использование: from core.db import get_db, init_db
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.config import settings


def _data_root(workspace: str | None = None) -> Path:
    # TRINITY_DATA_DIR > settings.data_dir > workspace/.trinity (compat)
    # Поддерживает os.environ напрямую чтобы не требовать реимпорта settings в тестах
    import os

    env_dir = os.environ.get("TRINITY_DATA_DIR") or getattr(settings, "data_dir", None)
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    ws = workspace or settings.workspace_dir
    return Path(ws).resolve() / ".trinity"


def db_path(workspace: str | None = None) -> Path:
    # workspace param deprecated for external data_dir — игнорируется когда TRINITY_DATA_DIR задан
    return _data_root(workspace) / "trinity.db"


def init_db(workspace: str | None = None) -> Path:
    p = db_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS rooms (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            strategy TEXT NOT NULL DEFAULT 'auto',
            builtin INTEGER NOT NULL DEFAULT 0,
            data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS changes (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            status TEXT NOT NULL,
            base_hash TEXT NOT NULL,
            content TEXT NOT NULL,
            diff TEXT NOT NULL,
            created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS history (
            session_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            data TEXT NOT NULL,
            PRIMARY KEY (session_id, idx)
        );
        CREATE INDEX IF NOT EXISTS idx_history_session ON history(session_id);
        """)
        # migrate: add op column for delete proposals (0.6.0)
        try:
            cur = con.execute("PRAGMA table_info(changes)")
            cols = {row[1] for row in cur.fetchall()}
            if "op" not in cols:
                con.execute("ALTER TABLE changes ADD COLUMN op TEXT NOT NULL DEFAULT 'write'")
        except Exception:
            pass
        # seed default room if empty
        cur = con.execute("SELECT COUNT(*) FROM rooms")
        if cur.fetchone()[0] == 0:
            con.execute("INSERT OR IGNORE INTO rooms (id, name, strategy, builtin, data) VALUES (?,?,?,?,?)",
                        ("general", "General", "auto", 1, json.dumps({"id":"general","name":"General","strategy":"auto","builtin":True})) )
        con.commit()
    finally:
        con.close()
    return p


def is_enabled(workspace: str | None = None) -> bool:
    # enabled if settings flag or db file already exists
    if getattr(settings, "use_sqlite", False):
        return True
    ws = workspace or settings.workspace_dir
    return db_path(ws).exists()


def migrate_json_if_needed(workspace: str | None = None) -> None:
    """One-time migration from JSON files -> sqlite (idempotent)."""
    if not is_enabled(workspace):
        return
    ws = workspace or settings.workspace_dir
    p = db_path(ws)
    init_db(ws)
    con = sqlite3.connect(str(p))
    try:
        # rooms.json
        rooms_json = Path(ws) / ".trinity" / "rooms.json"
        if rooms_json.exists():
            try:
                data = json.loads(rooms_json.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for r in data:
                        con.execute("INSERT OR IGNORE INTO rooms (id, name, strategy, builtin, data) VALUES (?,?,?,?,?)",
                                    (r.get("id"), r.get("name"), r.get("strategy","auto"), 1 if r.get("builtin") else 0, json.dumps(r, ensure_ascii=False)))
                con.commit()
            except Exception:
                pass
        # changes.json
        changes_json = Path(ws) / ".trinity" / "changes.json"
        if changes_json.exists():
            try:
                data = json.loads(changes_json.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for c in data:
                        con.execute("INSERT OR IGNORE INTO changes (id, path, status, base_hash, content, diff) VALUES (?,?,?,?,?,?)",
                                    (c.get("id"), c.get("path"), c.get("status","pending"), c.get("base_hash",""), c.get("content",""), c.get("diff","")))
                    con.commit()
            except Exception:
                pass
        # .trinity_sessions/*.json -> history
        sessions_dir = Path(ws) / ".trinity_sessions"
        if sessions_dir.exists():
            for jf in sessions_dir.glob("*.json"):
                sid = jf.stem
                try:
                    data = json.loads(jf.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        # only migrate if no rows yet for this session
                        cur = con.execute("SELECT COUNT(*) FROM history WHERE session_id=?", (sid,))
                        if cur.fetchone()[0] == 0:
                            for idx, msg in enumerate(data):
                                con.execute("INSERT OR IGNORE INTO history (session_id, idx, data) VALUES (?,?,?)",
                                            (sid, idx, json.dumps(msg, ensure_ascii=False)))
                    con.commit()
                except Exception:
                    pass
    finally:
        con.close()
