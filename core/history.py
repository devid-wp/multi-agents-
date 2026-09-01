import os
import json
import logging
import sqlite3
from typing import List, Optional
from pydantic import TypeAdapter

from core.models import ChatMessage
from core.config import settings

log = logging.getLogger("trinity.history")

from core.db import db_path, init_db

class HistoryManager:
    """
    Управляет сохранением и загрузкой истории диалогов в SQLite (.trinity/trinity.db).
    JSON fallback удалён в 0.6.0 — только SQLite.
    """

    def __init__(self, workspace_dir: Optional[str] = None):
        self.workspace_dir = workspace_dir or settings.workspace_dir
        self.sessions_dir = os.path.join(self.workspace_dir, ".trinity_sessions")
        os.makedirs(self.sessions_dir, exist_ok=True)
        
        # TypeAdapter для списка ChatMessage
        self._adapter = TypeAdapter(List[ChatMessage])

    def _get_path(self, session_id: str) -> str:
        # Простейшая валидация, чтобы избежать path traversal
        safe_id = "".join(c for c in session_id if c.isalnum() or c in ("-", "_"))
        if not safe_id:
            safe_id = "default"
        return os.path.join(self.sessions_dir, f"{safe_id}.json")

    def load(self, session_id: str) -> List[ChatMessage]:
        """Загружает историю диалога для указанной сессии (SQLite only)."""
        if not session_id:
            return []
        try:
            init_db(self.workspace_dir)
            con = sqlite3.connect(str(db_path(self.workspace_dir)))
            cur = con.execute("SELECT data FROM history WHERE session_id=? ORDER BY idx", (session_id,))
            rows = cur.fetchall()
            con.close()
            if rows:
                data = [json.loads(r[0]) for r in rows]
                return self._adapter.validate_python(data)
            return []
        except Exception as e:
            log.warning(f"SQLite load failed for {session_id}: {e}")
            return []

    def save(self, session_id: str, messages: List[ChatMessage]) -> None:
        """Сохраняет историю диалога для указанной сессии, применяя сжатие (Sliding Window), если сообщений слишком много."""
        if not session_id:
            return
            
        # --- SLIDING WINDOW COMPRESSION ---
        MAX_HISTORY = settings.history_max_messages
        if len(messages) > MAX_HISTORY:
            # Оставляем первые 5 сообщений (контекст задачи и первый план), 
            # и последние (MAX_HISTORY - 6) сообщений. 
            # Вырезанное заменяем одним системным сообщением.
            head = messages[:5]
            tail = messages[-(MAX_HISTORY - 6):]
            
            truncated_count = len(messages) - len(head) - len(tail)
            from core.models import ChatMessage, Role
            summary_msg = ChatMessage(
                role=Role.SYSTEM,
                content=f"[... {truncated_count} previous turns omitted to save context window ...]"
            )
            messages_to_save = head + [summary_msg] + tail
        else:
            messages_to_save = messages
            
        try:
            init_db(self.workspace_dir)
            con = sqlite3.connect(str(db_path(self.workspace_dir)))
            con.execute("DELETE FROM history WHERE session_id=?", (session_id,))
            data = [m.model_dump(mode="json") for m in messages_to_save]
            for idx, msg in enumerate(data):
                con.execute("INSERT INTO history (session_id, idx, data) VALUES (?,?,?)",
                            (session_id, idx, json.dumps(msg, ensure_ascii=False)))
            con.commit()
            con.close()
        except Exception as e:
            log.error(f"SQLite save failed for {session_id}: {e}")
