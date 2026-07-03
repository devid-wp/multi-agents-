"""
core/llm_clients.py
───────────────────
Тонкие async-клиенты для двух LLM-провайдеров:

  • NvidiaClient — NVIDIA NIM API (OpenAI-compatible)
  • OllamaClient — локальный Ollama (своё API, но похоже)

Оба клиента поддерживают:
  • обычную генерацию (chat)
  • стриминг (для SSE на фронтенд)
  • tool-calling (через JSON в ответе; формат Cline-подобный)

Если API временно недоступен — кидают LLMError с человеческим сообщением.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from core.config import settings
from core.models import ChatMessage, ToolCall

log = logging.getLogger("trinity.llm")


class LLMError(RuntimeError):
    """Любая ошибка взаимодействия с LLM-провайдером."""


# ───────────────────────────────────────────────────────────────────
# NVIDIA NIM
# ───────────────────────────────────────────────────────────────────
class NvidiaClient:
    """
    OpenAI-совместимый клиент для NVIDIA NIM.
    Docs: https://docs.api.nvidia.com/nim/reference/llm-api
    """

    def __init__(self, api_key: str, base_url: str = "https://integrate.api.nvidia.com/v1"):
        if not api_key:
            raise LLMError("NVIDIA API key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = settings.llm_timeout_seconds

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def chat(
        self,
        model: str,
        messages: List[ChatMessage],
        temperature: float = 0.6,
        max_tokens: int = 2048,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatMessage:
        """
        Один запрос → один ответ. Без стриминга.
        Удобно для Critic/Planner, которые отдают структурированный JSON.
        """
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [m.to_llm_dict() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
            except httpx.HTTPError as e:
                raise LLMError(f"NVIDIA network error: {e}") from e

        if resp.status_code != 200:
            raise LLMError(
                f"NVIDIA API {resp.status_code}: {resp.text[:500]}"
            )

        data = resp.json()
        try:
            choice = data["choices"][0]["message"]
            return ChatMessage(
                role=choice.get("role", "assistant"),
                content=choice.get("content") or "",
                tool_calls=choice.get("tool_calls"),
            )
        except (KeyError, IndexError) as e:
            raise LLMError(f"NVIDIA: malformed response: {e}; body={data}") from e


# ───────────────────────────────────────────────────────────────────
# Ollama (локально)
# ───────────────────────────────────────────────────────────────────
class OllamaClient:
    """
    Клиент локального Ollama.
    Docs: https://github.com/ollama/ollama/blob/main/docs/api.md

    Поддерживает как /api/chat (стриминг), так и обычный режим.
    """

    def __init__(self, base_url: str = "http://localhost:11434"):
        self._base_url = base_url.rstrip("/")
        self._timeout = settings.llm_timeout_seconds

    async def list_models(self) -> List[str]:
        """Возвращает список установленных локально моделей."""
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                resp = await client.get(f"{self._base_url}/api/tags")
            except httpx.HTTPError as e:
                raise LLMError(f"Ollama unreachable at {self._base_url}: {e}") from e
        if resp.status_code != 200:
            raise LLMError(f"Ollama tags {resp.status_code}: {resp.text[:300]}")
        return [m["name"] for m in resp.json().get("models", [])]

    async def chat(
        self,
        model: str,
        messages: List[ChatMessage],
        temperature: float = 0.3,
        max_tokens: int = 2048,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatMessage:
        """Один запрос → один ответ. Для Executor (часто вызывает tools)."""
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [m.to_llm_dict() for m in messages],
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    f"{self._base_url}/api/chat",
                    json=payload,
                )
            except httpx.HTTPError as e:
                raise LLMError(f"Ollama network error: {e}") from e

        if resp.status_code != 200:
            raise LLMError(
                f"Ollama API {resp.status_code}: {resp.text[:500]}"
            )

        data = resp.json()
        msg = data.get("message") or {}
        return ChatMessage(
            role=msg.get("role", "assistant"),
            content=msg.get("content") or "",
            tool_calls=msg.get("tool_calls"),
        )

    async def stream_chat(
        self,
        model: str,
        messages: List[ChatMessage],
        temperature: float = 0.3,
    ) -> AsyncGenerator[str, None]:
        """
        Стриминг токенов. Используется для Executor, чтобы пользователь
        видел, как агент «печатает».
        """
        payload = {
            "model": model,
            "messages": [m.to_llm_dict() for m in messages],
            "stream": True,
            "options": {"temperature": temperature},
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                async with client.stream(
                    "POST", f"{self._base_url}/api/chat", json=payload
                ) as resp:
                    if resp.status_code != 200:
                        text = await resp.aread()
                        raise LLMError(f"Ollama {resp.status_code}: {text[:300]!r}")
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        delta = chunk.get("message", {}).get("content") or ""
                        if delta:
                            yield delta
                        if chunk.get("done"):
                            break
            except httpx.HTTPError as e:
                raise LLMError(f"Ollama stream error: {e}") from e
