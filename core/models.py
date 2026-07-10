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

from pydantic import BaseModel, Field, field_validator, model_validator


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
    content: str = ""  # допустимо пустое значение, если есть tool_calls
    agent: Optional[AgentName] = None  # кто сгенерировал (для логов)
    timestamp: float = Field(default_factory=time.time)
    # Tool-calling
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    # Имя функции для tool-результата (нужно Gemini, требует functionResponse.name).
    # Не используется OpenAI/NVIDIA провайдерами; agent-цикл проставляет его
    # при сериализации результатов tools.js-порта в историю.
    name: Optional[str] = None
    # Метаданные (для дебага, не уходят в LLM)
    meta: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def _content_default_for_empty(cls, v):
        # Поле content теперь может быть пустой строкой — это допустимо
        # для assistant-сообщений, у которых есть tool_calls (OpenAI /
        # Ollama convention: при наличии tool_calls content может быть "").
        # Реальная проверка непустоты делается в model_validator
        # _content_required_unless_tools, который видит ВСЕ поля сразу.
        return v if v is not None else ""

    @model_validator(mode="after")
    def _content_required_unless_tools(self):
        # Финальная проверка content: пустой content допустим только если
        # у сообщения есть tool_calls. Иначе — ValueError.
        if not self.tool_calls:
            if not self.content or not str(self.content).strip():
                raise ValueError("content must not be empty")
        return self

    def to_llm_dict(self) -> Dict[str, Any]:
        """
        Сериализация для LLM API.
        Содержит только то, что понимает OpenAI-совместимый протокол.
        """
        out: Dict[str, Any] = {"role": self.role.value}
        # Если есть tool_calls — content может быть пустым/null
        # (Ollama и OpenAI принимают и "", и null в этом случае).
        if self.content or not self.tool_calls:
            out["content"] = self.content
        else:
            out["content"] = ""
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            out["tool_calls"] = self.tool_calls
        # `name` нужен только в tool-сообщениях, чтобы Gemini (и потенциально
        # OpenAI strict mode) знали, КАКАЯ функция выполнилась. Прочие роли
        # это поле игнорируют.
        if self.name and self.role == Role.TOOL:
            out["name"] = self.name
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
class AgentProviderConfig(BaseModel):
    provider: Literal["nvidia", "ollama", "gpt", "anthropic", "google"]
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model_name: Optional[str] = None


class SettingsPayload(BaseModel):
    """Тело POST /api/settings — данные из формы."""
    planner: Optional[AgentProviderConfig] = None
    executor: Optional[AgentProviderConfig] = None
    critic: Optional[AgentProviderConfig] = None


class AgentProviderResponse(BaseModel):
    provider: str
    has_key: bool
    key_masked: Optional[str] = None
    base_url: Optional[str] = None
    model_name: Optional[str] = None


class SettingsResponse(BaseModel):
    """Ответ GET /api/settings — для восстановления формы."""
    planner: Optional[AgentProviderResponse] = None
    executor: Optional[AgentProviderResponse] = None
    critic: Optional[AgentProviderResponse] = None


class ChatRequest(BaseModel):
    """Тело POST /api/chat — пользовательский промпт."""

    message: str = Field(..., min_length=1, max_length=20_000)
    # Опционально: идентификатор сессии для персистентности истории
    session_id: Optional[str] = None
    # Опционально: пользователь может сразу прислать кредентиалы,
    # не сохраняя их в сессии (для быстрых тестов)
    ephemeral_credentials: Optional[SettingsPayload] = None
    strategy: Optional[Literal["auto", "planner", "direct"]] = None


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
        "strategy",        # выбранная стратегия маршрутизации
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
