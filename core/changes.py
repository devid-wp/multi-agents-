from __future__ import annotations

import difflib
import hashlib
import json
import os
import uuid
from pathlib import Path
from threading import Lock  # sync store; FastAPI вызывает через asyncio.to_thread / sync context, поэтому threading.Lock корректен

import sqlite3

from tools.file_tool import _safe_resolve
try:
    from core.db import db_path, init_db, is_enabled
except ImportError:
    def is_enabled(ws=None): return False  # type: ignore
    def init_db(ws=None): return None  # type: ignore


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ChangeStore:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.path = Path(workspace) / ".trinity" / "changes.json"
        self._lock = Lock()

    def _load(self) -> list[dict]:
        if is_enabled(self.workspace):
            try:
                init_db(self.workspace)
                con = sqlite3.connect(str(db_path(self.workspace)))
                # handle op column may not exist in old DBs
                try:
                    cur = con.execute("SELECT id, path, status, base_hash, content, diff, op FROM changes ORDER BY rowid")
                    rows = cur.fetchall()
                    con.close()
                    return [{"id":r[0],"path":r[1],"status":r[2],"base_hash":r[3],"content":r[4],"diff":r[5],"op":r[6] if len(r)>6 else "write"} for r in rows]
                except Exception:
                    cur = con.execute("SELECT id, path, status, base_hash, content, diff FROM changes ORDER BY rowid")
                    rows = cur.fetchall()
                    con.close()
                    return [{"id":r[0],"path":r[1],"status":r[2],"base_hash":r[3],"content":r[4],"diff":r[5],"op":"write"} for r in rows]
            except Exception:
                pass
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save(self, changes: list[dict]) -> None:
        if is_enabled(self.workspace):
            try:
                init_db(self.workspace)
                con = sqlite3.connect(str(db_path(self.workspace)))
                con.execute("DELETE FROM changes")
                for c in changes[-100:]:
                    op = c.get("op", "write")
                    try:
                        con.execute("INSERT INTO changes (id, path, status, base_hash, content, diff, op) VALUES (?,?,?,?,?,?,?)",
                                    (c["id"], c["path"], c["status"], c["base_hash"], c["content"], c["diff"], op))
                    except Exception:
                        con.execute("INSERT INTO changes (id, path, status, base_hash, content, diff) VALUES (?,?,?,?,?,?)",
                                    (c["id"], c["path"], c["status"], c["base_hash"], c["content"], c["diff"]))
                con.commit()
                con.close()
                return
            except Exception:
                pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(changes, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def propose(self, relative_path: str, new_content: str) -> dict:
        target = _safe_resolve(self.workspace, relative_path)
        old_content = target.read_text(encoding="utf-8") if target.exists() else ""
        diff = "".join(difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
        ))
        proposal = {
            "id": uuid.uuid4().hex,
            "path": relative_path.replace("\\", "/"),
            "status": "pending",
            "base_hash": _digest(old_content),
            "content": new_content,
            "diff": diff,
            "op": "write",
        }
        with self._lock:
            changes = self._load()
            changes.append(proposal)
            self._save(changes[-100:])
        return proposal

    def propose_delete(self, relative_path: str) -> dict:
        target = _safe_resolve(self.workspace, relative_path)
        if not target.exists():
            raise ValueError(f"File not found: {relative_path}")
        if target.is_dir():
            raise ValueError(f"Cannot delete directory via delete_file: {relative_path}")
        old_content = target.read_text(encoding="utf-8") if target.is_file() else ""
        diff = "".join(difflib.unified_diff(
            old_content.splitlines(keepends=True),
            [],
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
        )) or f"--- a/{relative_path}\n+++ b/{relative_path}\n@@ -1 +0,0 @@\n-{old_content[:200]}\n"
        proposal = {
            "id": uuid.uuid4().hex,
            "path": relative_path.replace("\\", "/"),
            "status": "pending",
            "base_hash": _digest(old_content),
            "content": "",
            "diff": diff,
            "op": "delete",
        }
        with self._lock:
            changes = self._load()
            changes.append(proposal)
            self._save(changes[-100:])
        return proposal

    def list(self) -> list[dict]:
        with self._lock:
            return [{k: v for k, v in item.items() if k != "content"} for item in self._load()]

    def decide(self, proposal_id: str, approve: bool) -> dict:
        with self._lock:
            changes = self._load()
            proposal = next((item for item in changes if item["id"] == proposal_id), None)
            if proposal is None:
                raise ValueError("proposal not found")
            if proposal["status"] != "pending":
                raise ValueError("proposal already decided")
            if approve:
                target = _safe_resolve(self.workspace, proposal["path"])
                current = target.read_text(encoding="utf-8") if target.exists() else ""
                if _digest(current) != proposal["base_hash"]:
                    raise ValueError("file changed after preview; create a new proposal")
                if proposal.get("op") == "delete":
                    if target.exists():
                        target.unlink()
                    proposal["status"] = "applied"
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temp = target.with_suffix(target.suffix + ".trinity.tmp")
                    temp.write_text(proposal["content"], encoding="utf-8")
                    os.replace(temp, target)
                    proposal["status"] = "applied"
            else:
                proposal["status"] = "rejected"
            self._save(changes)
            return {k: v for k, v in proposal.items() if k != "content"}
