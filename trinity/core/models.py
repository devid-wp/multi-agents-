"""
core/models.py
──────────────
Pydantic-модели для строгой валидации сообщений и состояния системы.

Используются на каждом уровне — от входящих HTTP-запросов
до внутренней переписки агентов.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


# ───────────────────────────────────────────────────────────────────
# Роли и типы сообщений
# ───────────────────────────────────────────────────────────────────
class Role(str, Enum):
    """Chat-роли, совместимые с OpenAI/NVIDIA/Ollama."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"  # результат выполнения tool-call


class AgentName(str, Enum):
    """Идентификаторы агентов в системе."""

    PLANNER = "planner"
    CRITIC = "critic"
    EXECUTOR = "executor"
    MANAGER = "manager"  # для системных логов


# ───────────────────────────────────────────────────────────────────
# Одно сообщение в чате
# ───────────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    """
    Универсальное сообщение. Проходит между агентами и LLM-провайдерами.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    role: Role
    content: str
    agent: Optional[AgentName] = None  # кто сгенерировал (для логов)
    timestamp: float = Field(default_factory=time.time)
    # Tool-calling
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    # Метаданные (для дебага, не уходят в LLM)
    meta: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def _content_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("content must not be empty")
        return v

    def to_llm_dict(self) -> Dict[str, Any]:
        """
        Сериализация для LLM API.
        Содержит только то, что понимает OpenAI-совместимый протокол.
        """
        out: Dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            out["tool_calls"] = self.tool_calls
        return out


# ───────────────────────────────────────────────────────────────────
# Tool-вызовы (Cline-подобный формат)
# ───────────────────────────────────────────────────────────────────
class ToolCall(BaseModel):
    """Один вызов инструмента, инициированный агентом."""

    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:12]}")
    name: str = Field(..., min_length=1)
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Результат выполнения инструмента."""

    tool_call_id: str
    name: str
    success: bool
    output: str
    error: Optional[str] = None
    duration_ms: int = 0


# ───────────────────────────────────────────────────────────────────
# Запросы / ответы FastAPI
# ───────────────────────────────────────────────────────────────────
class SettingsPayload(BaseModel):
    """Тело POST /api/settings — данные из формы."""

    nvidia_api_key: Optional[str] = None
    ollama_url: Optional[str] = None
    planner_model: Optional[str] = None
    critic_model: Optional[str] = None
    executor_model: Optional[str] = None


class SettingsResponse(BaseModel):
    """Ответ GET /api/settings — для восстановления формы."""

    has_nvidia: bool
    ollama_url: str
    planner_model: str
    critic_model: str
    executor_model: str
    # Сам ключ никогда не возвращается клиенту!
    nvidia_key_masked: Optional[str] = None


class ChatRequest(BaseModel):
    """Тело POST /api/chat — пользовательский промпт."""

    message: str = Field(..., min_length=1, max_length=20_000)
    # Опционально: пользователь может сразу прислать кредентиалы,
    # не сохраняя их в сессию (для быстрых тестов)
    ephemeral_credentials: Optional[SettingsPayload] = None


# ───────────────────────────────────────────────────────────────────
# Событие прогресса (SSE-стрим)
# ───────────────────────────────────────────────────────────────────
class ProgressEvent(BaseModel):
    """
    Одно событие Server-Sent Events, отправляемое в браузер.
    Поле kind определяет, как фронтенд должен его отрисовать.
    """

    kind: Literal[
        "agent_start",     # агент начал работу
        "agent_message",   # текст от агента
        "tool_call",       # агент вызвал инструмент
        "tool_result",     # результат инструмента
        "agent_done",      # агент закончил
        "final",           # финальный ответ пользователю
        "error",           # ошибка
        "info",            # информационное сообщение
    ]
    agent: Optional[AgentName] = None
    content: Optional[str] = None
    tool: Optional[ToolCall] = None
    result: Optional[ToolResult] = None
    timestamp: float = Field(default_factory=time.time)

    def to_sse(self) -> str:
        """Форматирует в SSE-протокол (data: <json>\\n\\n)."""
        import json
        return f"data: {self.model_dump_json()}\n\n"
