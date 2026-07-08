import os
import json
import logging
from typing import List, Optional
from pydantic import TypeAdapter

from core.models import ChatMessage
from core.config import settings

log = logging.getLogger("trinity.history")

class HistoryManager:
    """
    Управляет сохранением и загрузкой истории диалогов в JSON файлы.
    Файлы хранятся в папке .trinity_sessions внутри workspace_dir.
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
        """Загружает историю диалога для указанной сессии."""
        if not session_id:
            return []
            
        path = self._get_path(session_id)
        if not os.path.exists(path):
            return []
            
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, list):
                    return []
                return self._adapter.validate_python(data)
        except Exception as e:
            log.warning(f"Ошибка при загрузке сессии {session_id}: {e}")
            return []

    def save(self, session_id: str, messages: List[ChatMessage]) -> None:
        """Сохраняет историю диалога для указанной сессии."""
        if not session_id:
            return
            
        path = self._get_path(session_id)
        try:
            # Сериализуем через pydantic, чтобы сохранить все поля (включая datetime/uuid)
            data = [m.model_dump(mode="json") for m in messages]
            
            # Пишем во временный файл и атомарно переименовываем
            temp_path = path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, path)
        except Exception as e:
            log.error(f"Ошибка при сохранении сессии {session_id}: {e}")
