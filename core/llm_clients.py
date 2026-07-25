"""
core/llm_clients.py
───────────────────
Тонкие async-клиенты для двух LLM-провайдеров:

  • NvidiaClient — NVIDIA NIM API (OpenAI-compatible)
  • OllamaClient — локальный Ollama (своё API, но похоже)
  • GoogleGeminiClient — клиент для Google AI Studio (Gemini)
  • AnthropicClient — клиент для Anthropic Claude API

Оба клиента поддерживают:
  • обычную генерацию (chat)
  • стриминг (для SSE на фронтенд)
  • tool-calling (через JSON в ответе; формат Cline-подобный)

Если API временно недоступен — кидают LLMError с человеческим сообщением.

NvidiaClient маршрутизирует запросы по AgentName: для Planner и Critic
могут быть разные ключи и base URL (NIM эндпоинты / квоты / провайдеры).
"""

from __future__ import annotations

import abc
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

import httpx

from core.config import settings
from core.models import AgentName, ChatMessage, ToolCall

log = logging.getLogger("trinity.llm")


import asyncio
from functools import wraps

class LLMError(RuntimeError):
    """Любая ошибка взаимодействия с LLM-провайдером."""

# ───────────────────────────────────────────────────────────────────
# Retry and Circuit Breaker logic
# ───────────────────────────────────────────────────────────────────
_global_consecutive_errors = 0
_CIRCUIT_BREAKER_THRESHOLD = 15

def with_retry_and_circuit_breaker(max_attempts: int = 3, backoff_delays: tuple = (1, 2, 4)):
    """
    Декоратор для LLM-вызовов:
      - 3 попытки (по умолчанию) с экспоненциальным backoff.
      - При 429/50x ошибках ждет и пробует снова (до исчерпания попыток).
      - Если глобально подряд много ошибок — открывает circuit breaker.
    В самом клиенте мы можем дополнительно переключать ключи перед ретраем.
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            global _global_consecutive_errors
            
            if _global_consecutive_errors >= _CIRCUIT_BREAKER_THRESHOLD:
                raise LLMError("Circuit breaker open: Too many consecutive LLM errors globally.")
                
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    res = await func(self, *args, **kwargs)
                    _global_consecutive_errors = 0  # Сброс при успехе
                    return res
                except Exception as e:
                    # Проверяем, нужно ли ретраить (обычно 429, 50x)
                    is_retryable = False
                    if "429" in str(e) or "503" in str(e) or "502" in str(e) or "504" in str(e) or "NetworkError" in str(e):
                        is_retryable = True
                        
                    if not is_retryable or attempt == max_attempts - 1:
                        _global_consecutive_errors += 1
                        raise e
                    
                    delay = backoff_delays[attempt] if attempt < len(backoff_delays) else backoff_delays[-1]
                    log.warning(f"LLM call failed with {e}. Retrying in {delay}s (attempt {attempt + 1}/{max_attempts})...")
                    
                    # Если клиент поддерживает ротацию ключей (например, NvidiaProvider),
                    # мы можем запросить её:
                    agent = kwargs.get('agent')
                    if hasattr(self, '_resolve') and agent:
                        provider = self._resolve(agent)
                        if hasattr(provider, 'rotate_key'):
                            provider.rotate_key()
                            
                    await asyncio.sleep(delay)
                    
            raise last_exc
        return wrapper
    return decorator


class BaseLLMClient(abc.ABC):
    """Универсальный интерфейс для LLM-провайдеров."""

    @abc.abstractmethod
    async def chat(
        self,
        *,
        model: str,
        messages: List[ChatMessage],
        temperature: float = 0.6,
        max_tokens: int = 2048,
        tools: Optional[List[Dict[str, Any]]] = None,
        agent: Optional[AgentName] = None,
    ) -> ChatMessage:
        raise NotImplementedError


class OpenAICompatibleClient(BaseLLMClient):
    """Базовая реализация для Ollama/vLLM/OpenRouter/OpenAI-совместимых провайдеров."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = "http://localhost:11434/v1",
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.default_model = model or ""
        self._timeout = timeout or settings.llm_timeout_seconds

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @with_retry_and_circuit_breaker()
    async def chat(
        self,
        *,
        model: str,
        messages: List[ChatMessage],
        temperature: float = 0.6,
        max_tokens: int = 2048,
        tools: Optional[List[Dict[str, Any]]] = None,
        agent: Optional[AgentName] = None,
    ) -> ChatMessage:
        payload: Dict[str, Any] = {
            "model": model or self.default_model,
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
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
            except httpx.HTTPError as exc:
                raise LLMError(f"OpenAI-compatible request failed: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"OpenAI-compatible API {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        choice = data["choices"][0]["message"]
        return ChatMessage(
            role=choice.get("role", "assistant"),
            content=choice.get("content") or "",
            tool_calls=choice.get("tool_calls"),
        )


# ───────────────────────────────────────────────────────────────────
# NVIDIA NIM — конфиг одного провайдера
# ───────────────────────────────────────────────────────────────────
@dataclass
class NvidiaProvider:
    """
    Конфигурация одного NVIDIA-эндпоинта.
    """

    api_key: str
    base_url: str
    model_url: Optional[str] = None

    # Внутренний пул ключей
    _keys: List[str] = None  # type: ignore
    _current_key_idx: int = 0

    # Маркер «хвоста», который клиент дописывает к base_url.
    _CHAT_COMPLETIONS_SUFFIX = "/chat/completions"

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_key.strip():
            raise LLMError("NvidiaProvider: api_key is empty")
        # Парсим ключи, разделенные запятой
        self._keys = [k.strip() for k in self.api_key.split(",") if k.strip()]
        if not self._keys:
            raise LLMError("NvidiaProvider: no valid keys found")
        self.api_key = self._keys[0]
        
        self.base_url = (self.base_url or "https://integrate.api.nvidia.com/v1").rstrip("/")
        if self.model_url:
            self.model_url = self.model_url.strip().rstrip("/")

    def _is_valid_model_override(self) -> bool:
        if not self.model_url:
            return False
        if len(self.model_url) <= len(self.base_url):
            return False
        if self.model_url.endswith(self._CHAT_COMPLETIONS_SUFFIX):
            return True
        if "/models/" in self.model_url and self.model_url.endswith("/infer"):
            return True
        return False

    def endpoint_url(self) -> str:
        if self.model_url:
            if self._is_valid_model_override():
                result = self.model_url
                print(f"DEBUG_URL: final_url={result} (source=model_url override, base_url={self.base_url})")
                return result
            print(f"WARNING_URL: model_url={self.model_url!r} is not a valid override. Falling back to base_url.")

        if self.base_url.endswith(self._CHAT_COMPLETIONS_SUFFIX):
            result = self.base_url
            print(f"DEBUG_URL: final_url={result} (source=base_url as-is, already contains /chat/completions)")
            return result

        result = f"{self.base_url}{self._CHAT_COMPLETIONS_SUFFIX}"
        print(f"DEBUG_URL: final_url={result} (source=base_url + appended /chat/completions)")
        return result

    def get_current_key(self) -> str:
        return self._keys[self._current_key_idx]

    def rotate_key(self) -> None:
        if len(self._keys) > 1:
            old_key = self._keys[self._current_key_idx]
            self._current_key_idx = (self._current_key_idx + 1) % len(self._keys)
            new_key = self._keys[self._current_key_idx]
            print(f"DEBUG_KEY: rotated key from {old_key[:6]}... to {new_key[:6]}...")


NvidiaProviderResolver = Callable[[AgentName], Tuple[Any, ...]]


# ───────────────────────────────────────────────────────────────────
# NVIDIA NIM Client
# ───────────────────────────────────────────────────────────────────
class NvidiaClient(BaseLLMClient):
    """OpenAI-совместимый клиент для NVIDIA NIM с маршрутизацией по AgentName."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://integrate.api.nvidia.com/v1",
        providers: Optional[Dict[AgentName, Tuple[Any, ...]]] = None,
        provider_resolver: Optional[NvidiaProviderResolver] = None,
    ):
        try:
            if providers:
                for _agent, _entry in providers.items():
                    if isinstance(_entry, tuple) and len(_entry) == 3:
                        _k, _b, _m = _entry
                    else:
                        _k, _b = _entry  # type: ignore[misc]
                        _m = None
                    print(f"DEBUG_INIT: agent={_agent.value} base_url={_b!r} model_url={_m!r}")
            else:
                print(f"DEBUG_INIT: base_url={base_url!r} model_url=None")
        except Exception as _e:
            print(f"DEBUG_INIT: failed to log input: {_e!r}")

        if providers is None and provider_resolver is None and api_key:
            _norm = (base_url or "").rstrip("/")
            if _norm.endswith("/v1"):
                base_url = _norm + "/chat/completions"
                print(f"DEBUG_INIT: REWRITE bare /v1 -> {base_url!r} (model_url is empty, single-provider mode)")

        self._timeout = settings.llm_timeout_seconds
        self._providers: Dict[AgentName, NvidiaProvider] = {}
        self._resolver = None

        if providers:
            for agent, entry in providers.items():
                if isinstance(entry, tuple) and len(entry) == 3:
                    key, url, model_url = entry
                else:
                    key, url = entry  # type: ignore[misc]
                    model_url = None
                if (url or "").rstrip("/").endswith("/v1") and not model_url:
                    url = url.rstrip("/") + "/chat/completions"
                    print(f"DEBUG_INIT: REWRITE bare /v1 for {agent.value} -> {url!r}")
                self._providers[agent] = NvidiaProvider(
                    api_key=key, base_url=url, model_url=model_url,
                )
        elif provider_resolver is not None:
            self._resolver = provider_resolver
        elif api_key:
            self._providers[AgentName.MANAGER] = NvidiaProvider(
                api_key=api_key, base_url=base_url
            )
        else:
            raise LLMError("NvidiaClient: нужно передать providers, provider_resolver или api_key")

    def _resolve(self, agent: AgentName) -> NvidiaProvider:
        if self._resolver is not None:
            entry = self._resolver(agent)
            if isinstance(entry, tuple) and len(entry) == 3:
                key, url, model_url = entry
            else:
                key, url = entry  # type: ignore[misc]
                model_url = None
            return NvidiaProvider(api_key=key, base_url=url, model_url=model_url)
        if agent in self._providers:
            return self._providers[agent]
        if AgentName.MANAGER in self._providers:
            return self._providers[AgentName.MANAGER]
        raise LLMError(f"NVIDIA: не сконфигурирован провайдер для агента '{agent.value}'. Доступные: {[a.value for a in self._providers]}")

    def _headers(self, provider: NvidiaProvider) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {provider.get_current_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @with_retry_and_circuit_breaker()
    async def chat(
        self,
        model: str,
        messages: List[ChatMessage],
        temperature: float = 0.6,
        max_tokens: int = 2048,
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        agent: AgentName = AgentName.MANAGER,
    ) -> ChatMessage:
        provider = self._resolve(agent)
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [m.to_llm_dict() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools

        url = provider.endpoint_url()
        headers = self._headers(provider)
        masked_headers = {
            **headers,
            "Authorization": f"Bearer {provider.api_key[:6]}...{provider.api_key[-4:]}",
        }
        print(f"[NVIDIA->{agent.value}] POST {url}")
        print(f"[NVIDIA->{agent.value}] payload: {json.dumps(payload, ensure_ascii=False)[:1500]}")
        print(f"DEBUG_HEADERS: {list(headers.keys())}")
        print(f"DEBUG_PAYLOAD_MODEL: {payload.get('model')}")
        if tools:
            print(f"DEBUG_TOOLS: {len(tools)} tool schema(s) sent: {[t.get('function', {}).get('name', '?') for t in tools]}")

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    url,
                    headers=self._headers(provider),
                    json=payload,
                )
            except httpx.HTTPError as e:
                raise LLMError(f"NVIDIA network error ({agent.value} @ {url}): {e}") from e

        if resp.status_code != 200:
            print(f"[NVIDIA←{agent.value}] HTTP {resp.status_code} for URL: {url}\n[NVIDIA←{agent.value}] response body: {resp.text[:500]}")
            raise LLMError(f"NVIDIA API {resp.status_code} ({agent.value}) @ {url}: {resp.text[:500]}")

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
# Ollama Client
# ───────────────────────────────────────────────────────────────────
class OllamaClient(BaseLLMClient):
    """Клиент локального Ollama."""

    def __init__(self, base_url: str = "http://localhost:11434", default_model: Optional[str] = None):
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model or ""
        self._timeout = settings.llm_timeout_seconds

    async def list_models(self) -> List[str]:
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                resp = await client.get(f"{self._base_url}/api/tags")
            except httpx.HTTPError as e:
                raise LLMError(f"Ollama unreachable at {self._base_url}: {e}") from e
        if resp.status_code != 200:
            raise LLMError(f"Ollama tags {resp.status_code}: {resp.text[:300]}")
        return [m["name"] for m in resp.json().get("models", [])]

    @with_retry_and_circuit_breaker()
    async def chat(
        self,
        *,
        model: str,
        messages: List[ChatMessage],
        temperature: float = 0.3,
        max_tokens: int = 2048,
        tools: Optional[List[Dict[str, Any]]] = None,
        agent: Optional[AgentName] = None,
    ) -> ChatMessage:
        payload: Dict[str, Any] = {
            "model": model or self._default_model,
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
                detail = str(e) or type(e).__name__
                raise LLMError(f"Ollama network error ({type(e).__name__}): {detail}") from e

        if resp.status_code != 200:
            raise LLMError(f"Ollama API {resp.status_code}: {resp.text[:500]}")

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
        payload = {
            "model": model,
            "messages": [m.to_llm_dict() for m in messages],
            "stream": True,
            "options": {"temperature": temperature},
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                async with client.stream("POST", f"{self._base_url}/api/chat", json=payload) as resp:
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


# ───────────────────────────────────────────────────────────────────
# Google Gemini Client
# ───────────────────────────────────────────────────────────────────
class GoogleGeminiClient(BaseLLMClient):
    """Клиент для Google AI Studio (Gemini)."""

    def __init__(self, api_key: str, timeout: Optional[float] = None) -> None:
        if not api_key or not api_key.strip():
            raise LLMError("Google Gemini API key is missing")
        # Вычищаем абсолютно все скрытые пробелы, табы и переносы из ключа
        self.api_key = "".join(api_key.split())
        self._timeout = timeout or settings.llm_timeout_seconds
        # Возвращаем v1beta, так как gemini-1.5 сидит там
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    @with_retry_and_circuit_breaker()
    async def chat(
        self,
        *,
        model: str,
        messages: List[ChatMessage],
        temperature: float = 0.6,
        max_tokens: int = 2048,
        tools: Optional[List[Dict[str, Any]]] = None,
        agent: Optional[AgentName] = None,
    ) -> ChatMessage:
        # ── ХИРУРГИЧЕСКИЙ ФИКС ИМЕНИ МОДЕЛИ ДЛЯ GEMINI ────────────────
        model = (model or "").strip()
        
        if not model or "llama" in model.lower():
            model = "gemini-1.5-flash"
        
        if model.startswith("models/"):
            model = model.replace("models/", "", 1)
            
        print(f"[GEMINI_FIX] Итоговый URL собран под модель: {model!r}")
        # ─────────────────────────────────────────────────────────────

        system_instruction = None
        contents: List[Dict[str, Any]] = []
        pending_tool_parts: List[Dict[str, Any]] = []

        def _flush_tool_parts() -> None:
            nonlocal pending_tool_parts
            if pending_tool_parts:
                contents.append({"role": "user", "parts": pending_tool_parts})
                pending_tool_parts = []

        for m in messages:
            if m.role == "system":
                if system_instruction is None:
                    system_instruction = {"parts": [{"text": m.content}]}
                else:
                    system_instruction["parts"].append({"text": m.content})
                _flush_tool_parts()
            elif m.role == "assistant":
                _flush_tool_parts()
                parts: List[Dict[str, Any]] = []
                if m.content:
                    parts.append({"text": m.content})
                if m.tool_calls:
                    for tc in m.tool_calls:
                        func_name = ""
                        func_args: Dict[str, Any] = {}
                        if isinstance(tc, dict):
                            func = tc.get("function", {})
                            func_name = func.get("name", "")
                            args_raw = func.get("arguments", {})
                        else:
                            func = getattr(tc, "function", None)
                            if func:
                                func_name = getattr(func, "name", "")
                                args_raw = getattr(func, "arguments", {})
                            else:
                                continue

                        if isinstance(args_raw, str):
                            try:
                                func_args = json.loads(args_raw)
                            except json.JSONDecodeError:
                                func_args = {}
                        else:
                            func_args = args_raw

                        parts.append({
                            "functionCall": {
                                "name": func_name,
                                "args": func_args
                            }
                        })
                if parts:
                    contents.append({"role": "model", "parts": parts})
            elif m.role == "tool":
                func_name = m.name or ""
                if not func_name:
                    log.warning("Tool message without function name (tool_call_id=%s) — Gemini may reject.", m.tool_call_id)
                response_payload: Dict[str, Any] = {
                    "output": m.content or "",
                }
                if isinstance(m.content, str) and m.content.startswith("ERROR:"):
                    response_payload["error"] = m.content
                pending_tool_parts.append({
                    "functionResponse": {
                        "name": func_name,
                        "response": response_payload
                    }
                })
            else:
                _flush_tool_parts()
                contents.append({"role": "user", "parts": [{"text": m.content}]})

        _flush_tool_parts()

        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            }
        }

        if system_instruction:
            payload["systemInstruction"] = system_instruction

        if tools:
            gemini_tools: List[Dict[str, Any]] = []
            for t in tools:
                if not isinstance(t, dict):
                    continue
                if t.get("type") == "function":
                    func = t.get("function", {})
                    gemini_tools.append({
                        "name": func.get("name", ""),
                        "description": func.get("description", ""),
                        "parameters": func.get("parameters", {}),
                    })
                elif "functionDeclarations" in t:
                    for fd in t["functionDeclarations"] or []:
                        if isinstance(fd, dict):
                            gemini_tools.append({
                                "name": fd.get("name", ""),
                                "description": fd.get("description", ""),
                                "parameters": fd.get("parameters", {}),
                            })
                elif "name" in t and "parameters" in t:
                    gemini_tools.append({
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {}),
                    })
            if gemini_tools:
                payload["tools"] = [{"functionDeclarations": gemini_tools}]

        url = f"{self.base_url}/{model}:generateContent?key={self.api_key}"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                )
            except httpx.HTTPError as exc:
                raise LLMError(f"Google Gemini request failed: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(f"Google Gemini API {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        
        try:
            candidate = data["candidates"][0]
            parts = candidate["content"]["parts"]
        except (KeyError, IndexError):
            return ChatMessage(role="assistant", content="", tool_calls=None)

        text = ""
        tool_calls = []

        for part in parts:
            if "text" in part:
                text += part["text"]
            if "functionCall" in part:
                fc = part["functionCall"]
                args = fc.get("args", {})
                args_str = json.dumps(args) if isinstance(args, dict) else str(args)
                
                tool_calls.append({
                    "type": "function",
                    "function": {
                        "name": fc.get("name", ""),
                        "arguments": args_str
                    }
                })

        return ChatMessage(
            role="assistant",
            content=text,
            tool_calls=tool_calls if tool_calls else None
        )

# ───────────────────────────────────────────────────────────────────
# Anthropic Client
# ───────────────────────────────────────────────────────────────────
class AnthropicClient(BaseLLMClient):
    """Клиент для Anthropic Claude API."""

    def __init__(self, api_key: str, timeout: Optional[float] = None) -> None:
        if not api_key or not api_key.strip():
            raise LLMError("Anthropic API key is missing")
        self.api_key = api_key.strip()
        self._timeout = timeout or settings.llm_timeout_seconds
        self.base_url = "https://api.anthropic.com/v1/messages"

    @with_retry_and_circuit_breaker()
    async def chat(
        self,
        *,
        model: str,
        messages: List[ChatMessage],
        temperature: float = 0.6,
        max_tokens: int = 4096,
        tools: Optional[List[Dict[str, Any]]] = None,
        agent: Optional[AgentName] = None,
    ) -> ChatMessage:
        system_instruction = ""
        anthropic_msgs = []
        
        for m in messages:
            if m.role == "system":
                system_instruction += m.content + "\n"
            elif m.role == "assistant":
                content_blocks = []
                if m.content:
                    content_blocks.append({"type": "text", "text": m.content})
                if m.tool_calls:
                    for tc in m.tool_calls:
                        func = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", None)
                        if not func:
                            continue
                        name = func.get("name", "") if isinstance(func, dict) else getattr(func, "name", "")
                        args_raw = func.get("arguments", {}) if isinstance(func, dict) else getattr(func, "arguments", {})
                        
                        func_args = {}
                        if isinstance(args_raw, str):
                            try:
                                func_args = json.loads(args_raw)
                            except json.JSONDecodeError:
                                pass
                        else:
                            func_args = args_raw
                            
                        tool_use_id = f"call_{hash(name + str(func_args))}"
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": name,
                            "input": func_args
                        })
                if content_blocks:
                    anthropic_msgs.append({"role": "assistant", "content": content_blocks})
            else:
                anthropic_msgs.append({"role": "user", "content": m.content})

        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": anthropic_msgs
        }
        
        if system_instruction:
            payload["system"] = system_instruction.strip()
            
        if tools:
            anthropic_tools = []
            for t in tools:
                if t.get("type") == "function":
                    func = t.get("function", {})
                    anthropic_tools.append({
                        "name": func.get("name", ""),
                        "description": func.get("description", ""),
                        "input_schema": func.get("parameters", {})
                    })
            if anthropic_tools:
                payload["tools"] = anthropic_tools

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    self.base_url,
                    json=payload,
                    headers=headers
                )
            except httpx.HTTPError as exc:
                raise LLMError(f"Anthropic request failed: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(f"Anthropic API {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        text = ""
        tool_calls = []
        
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                args_dict = block.get("input", {})
                tool_calls.append({
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(args_dict)
                    }
                })

        return ChatMessage(
            role="assistant",
            content=text,
            tool_calls=tool_calls if tool_calls else None
        )


# ───────────────────────────────────────────────────────────────────
# OpenRouter Client (Полностью независимый от родительских багов)
# ───────────────────────────────────────────────────────────────────
class OpenRouterClient(OpenAICompatibleClient):
    """
    Клиент для OpenRouter. Работает напрямую из Украины, предоставляет
    бесплатный доступ к мощным моделям (Llama 3.3 70B, Gemma 2 9B).
    """

    def __init__(self, api_key: str, timeout: Optional[float] = None, *args, **kwargs) -> None:
        if not api_key or not api_key.strip():
            raise LLMError("OpenRouter API key is missing")
        
        clean_key = "".join(api_key.split())
        
        super().__init__(
            api_key=clean_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=timeout
        )

    def _headers(self) -> Dict[str, str]:
        headers = super()._headers()
        headers["HTTP-Referer"] = "https://github.com/david/trinity"
        headers["X-Title"] = "Trinity Multi-Agent OS"
        return headers

    @with_retry_and_circuit_breaker()
    async def chat(
        self,
        *,
        model: str,
        messages: List[ChatMessage],
        temperature: float = 0.6,
        max_tokens: int = 2048,
        tools: Optional[List[Dict[str, Any]]] = None,
        agent: Optional[AgentName] = None,
    ) -> ChatMessage:
        model = (model or "").strip()

        # Автоматическая замена на новейший, гарантированно свободный фришный стек моделей
        if not model or "llama" in model.lower() or "gemini" in model.lower():
            if agent in (AgentName.PLANNER, AgentName.CRITIC):
                # Накатываем Llama 3.3 70B Free — у неё живые эндпоинты
                model = "meta-llama/llama-3.3-70b-instruct:free"
            else:
                model = "google/gemma-2-9b-it:free"

        print(f"[OPENROUTER_ROUTER] Agent: {agent.value if agent else 'UNKNOWN'} -> Model: {model!r}")

        payload: Dict[str, Any] = {
            "model": model,
            "messages": [m.to_llm_dict() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools

        exact_url = "https://openrouter.ai/api/v1/chat/completions"

        async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
            try:
                resp = await client.post(
                    exact_url,
                    headers=self._headers(),
                    json=payload,
                )
            except httpx.HTTPError as exc:
                raise LLMError(f"OpenRouter request failed: {exc}") from exc
                
        if resp.status_code != 200:
            print(f"[OPENROUTER_ERROR_BODY]: {resp.text}")
            raise LLMError(f"OpenRouter API {resp.status_code}: {resp.text[:500]}")
            
        data = resp.json()
        choice = data["choices"][0]["message"]
        return ChatMessage(
            role=choice.get("role", "assistant"),
            content=choice.get("content") or "",
            tool_calls=choice.get("tool_calls"),
        )