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

    Поля:
      • api_key     — bearer-токен
      • base_url    — OpenAI-совместимый эндпоинт (по умолчанию
                      {base_url}/chat/completions)
      • model_url   — опциональный ПОЛНЫЙ URL, переопределяющий эндпоинт
                      целиком (для случаев, когда провайдер отдаёт модель
                      по нестандартному пути, например NIM catalog endpoint
                      вида https://integrate.api.nvidia.com/v1/models/{model}/infer
                      или отдельный деплоймент)
    """

    api_key: str
    base_url: str
    model_url: Optional[str] = None

    # Внутренний пул ключей
    _keys: List[str] = None  # type: ignore
    _current_key_idx: int = 0

    # Маркер «хвоста», который клиент дописывает к base_url.
    # Если base_url уже заканчивается на него — не дублируем.
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
        """
        Является ли `model_url` валидным override-ом, который можно использовать
        вместо `base_url + /chat/completions`.

        Валидным считается override, который:
          • задан и непустой
          • строго ДЛИННЕЕ base_url (защита от случая, когда model_url ==
            base_url — тогда это не override, а дубль, который вернёт
            404, потому что по пути `…/v1` нет POST-handler-а)
          • выглядит как реальный endpoint одной из поддерживаемых форм:
              - OpenAI-compatible:    …/v1/chat/completions
              - NIM catalog endpoint: …/v1/models/{model}/infer
                (см. https://docs.api.nvidia.com/nim/reference/llm-api)

        Только корень API вроде `…/v1` или промежуточный путь `…/v1/models`
        (без указания конкретной модели и без /chat/completions) —
        отбрасываются как «плохие» override-ы.
        """
        if not self.model_url:
            return False
        if len(self.model_url) <= len(self.base_url):
            return False
        # OpenAI-compatible endpoint
        if self.model_url.endswith(self._CHAT_COMPLETIONS_SUFFIX):
            return True
        # NIM catalog endpoint: …/v1/models/{model}/infer
        if "/models/" in self.model_url and self.model_url.endswith("/infer"):
            return True
        return False

    def endpoint_url(self) -> str:
        """
        Полный URL, по которому пойдёт запрос.

        Правила (порядок проверок важен — от более специфичного к общему):

          1. Если `model_url` задан И валиден (содержит `/chat/completions`
             и длиннее `base_url`) — используем его как полный override.
             Возвращаем as-is, никаких хвостов не дописываем.
             Если `model_url` задан, но НЕ валиден (например, равен base_url
             или короче корня API) — печатаем WARNING и идём дальше
             по ветке base_url. Это защищает от «плохого» override-а,
             который отправляет запросы в пустоту.
          2. Если `base_url` уже заканчивается на `/chat/completions` —
             это УЖЕ полный эндпоинт. Возвращаем as-is, идемпотентно.
             Никаких дублирований вида `…/v1/chat/completions/chat/completions`.
          3. Иначе (типичный случай `…/v1`) — дописываем суффикс ровно один раз.
        """
        # 1) Полный override через model_url — только если он ВАЛИДЕН
        if self.model_url:
            if self._is_valid_model_override():
                result = self.model_url
                print(
                    f"DEBUG_URL: final_url={result}  "
                    f"(source=model_url override, base_url={self.base_url})"
                )
                return result
            # model_url задан, но это «плохой» override (совпадает с корнем
            # API, короче base_url, или не содержит /chat/completions).
            # Не даём ему сломать запрос — идём по ветке base_url.
            print(
                f"WARNING_URL: model_url={self.model_url!r} is not a valid "
                f"override (must contain '/chat/completions' AND be longer "
                f"than base_url={self.base_url!r}). Falling back to base_url."
            )

        # 2) Пользователь уже ввёл полный URL — НЕ трогаем его, не дописываем
        if self.base_url.endswith(self._CHAT_COMPLETIONS_SUFFIX):
            result = self.base_url
            print(
                f"DEBUG_URL: final_url={result}  "
                f"(source=base_url as-is, already contains /chat/completions)"
            )
            return result

        # 3) Стандартный случай: base_url = …/v1 → дописываем суффикс один раз
        result = f"{self.base_url}{self._CHAT_COMPLETIONS_SUFFIX}"
        print(
            f"DEBUG_URL: final_url={result}  "
            f"(source=base_url + appended /chat/completions)"
        )
        return result

    def get_current_key(self) -> str:
        """Возвращает текущий API-ключ из пула."""
        return self._keys[self._current_key_idx]

    def rotate_key(self) -> None:
        """Переключает на следующий ключ в пуле (round-robin)."""
        if len(self._keys) > 1:
            old_key = self._keys[self._current_key_idx]
            self._current_key_idx = (self._current_key_idx + 1) % len(self._keys)
            new_key = self._keys[self._current_key_idx]
            print(f"DEBUG_KEY: rotated key from {old_key[:6]}... to {new_key[:6]}...")


# Тип для «ленивого» провайдера (на случай, если креды меняются в рантайме).
# Возвращает (api_key, base_url[, model_url]).
NvidiaProviderResolver = Callable[[AgentName], Tuple[Any, ...]]


# ───────────────────────────────────────────────────────────────────
# NVIDIA NIM
# ───────────────────────────────────────────────────────────────────
class NvidiaClient(BaseLLMClient):
    """
    OpenAI-совместимый клиент для NVIDIA NIM с маршрутизацией по AgentName.
    Docs: https://docs.api.nvidia.com/nim/reference/llm-api

    Использование:
        client = NvidiaClient(providers={
            AgentName.PLANNER: ("nvapi-PLAN", "https://integrate.api.nvidia.com/v1"),
            AgentName.CRITIC:  ("nvapi-CRIT", "https://other.nim.example/v1"),
        })
        await client.chat(agent=AgentName.PLANNER, model=..., messages=...)

    Совместимость со старым кодом:
        - если providers=None и задан api_key/base_url, клиент работает
          в «один провайдер на всех» режиме (по умолчанию для AgentName.MANAGER);
        - параметр agent у chat() опционален, по умолчанию AgentName.MANAGER.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://integrate.api.nvidia.com/v1",
        providers: Optional[Dict[AgentName, Tuple[Any, ...]]] = None,
        provider_resolver: Optional[NvidiaProviderResolver] = None,
    ):
        # ── DEBUG_INIT: что реально долетело до конструктора ───────
        # Печатаем ДО любых нормализаций, чтобы видеть ровно то значение,
        # которое пришло из manager.py / UI / cookie-сессии.
        try:
            if providers:
                for _agent, _entry in providers.items():
                    if isinstance(_entry, tuple) and len(_entry) == 3:
                        _k, _b, _m = _entry
                    else:
                        _k, _b = _entry  # type: ignore[misc]
                        _m = None
                    print(
                        f"DEBUG_INIT: agent={_agent.value} "
                        f"base_url={_b!r} model_url={_m!r}"
                    )
            else:
                print(f"DEBUG_INIT: base_url={base_url!r} model_url=None")
        except Exception as _e:  # noqa: BLE001 — диагностика не должна падать
            print(f"DEBUG_INIT: failed to log input: {_e!r}")

        # ── Принудительный rewrite «голого /v1» ─────────────────────
        # Если в UI ввели только корень API (…/v1) и НЕ задали model_url —
        # добиваем /chat/completions прямо в base_url, чтобы запрос гарантированно
        # ушёл на реальный эндпоинт, а не на корень, который вернёт 404.
        # Это срабатывает ТОЛЬКО в режиме single-provider (api_key задан).
        if providers is None and provider_resolver is None and api_key:
            _norm = (base_url or "").rstrip("/")
            if _norm.endswith("/v1"):
                base_url = _norm + "/chat/completions"
                print(
                    f"DEBUG_INIT: REWRITE bare /v1 -> {base_url!r} "
                    f"(model_url is empty, single-provider mode)"
                )

        self._timeout = settings.llm_timeout_seconds

        # Режим «multi-provider»: словарь AgentName → (api_key, base_url[, model_url])
        # Поддерживаем оба формата для обратной совместимости.
        self._providers: Dict[AgentName, NvidiaProvider] = {}
        if providers:
            for agent, entry in providers.items():
                if isinstance(entry, tuple) and len(entry) == 3:
                    key, url, model_url = entry
                else:
                    key, url = entry  # type: ignore[misc]
                    model_url = None
                # Тот же rewrite для multi-provider-режима: если в UI прилетел
                # «голый» …/v1, добиваем /chat/completions. Но ТОЛЬКО при пустом
                # model_url, чтобы не сломать легитимный override.
                if (url or "").rstrip("/").endswith("/v1") and not model_url:
                    url = url.rstrip("/") + "/chat/completions"
                    print(
                        f"DEBUG_INIT: REWRITE bare /v1 for {agent.value} -> {url!r}"
                    )
                self._providers[agent] = NvidiaProvider(
                    api_key=key, base_url=url, model_url=model_url,
                )
        elif provider_resolver is not None:
            self._resolver = provider_resolver
        elif api_key:
            # Режим «single-provider» (back-compat): один ключ для всех агентов
            self._providers[AgentName.MANAGER] = NvidiaProvider(
                api_key=api_key, base_url=base_url
            )
            self._resolver = None
        else:
            raise LLMError(
                "NvidiaClient: нужно передать providers, provider_resolver или api_key"
            )

        self._resolver: Optional[NvidiaProviderResolver] = (
            provider_resolver if providers is None else None
        )

    # ── Резолвер провайдера по агенту ─────────────────────────────
    def _resolve(self, agent: AgentName) -> NvidiaProvider:
        """Возвращает конфиг провайдера для данного агента."""
        # 1) Ленивый резолвер
        if self._resolver is not None:
            entry = self._resolver(agent)
            if isinstance(entry, tuple) and len(entry) == 3:
                key, url, model_url = entry
            else:
                key, url = entry  # type: ignore[misc]
                model_url = None
            return NvidiaProvider(api_key=key, base_url=url, model_url=model_url)
        # 2) Прямой dict
        if agent in self._providers:
            return self._providers[agent]
        # 3) Fallback на MANAGER (single-provider режим)
        if AgentName.MANAGER in self._providers:
            return self._providers[AgentName.MANAGER]
        raise LLMError(
            f"NVIDIA: не сконфигурирован провайдер для агента '{agent.value}'. "
            f"Доступные: {[a.value for a in self._providers]}"
        )

    def _headers(self, provider: NvidiaProvider) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {provider.get_current_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── chat() — основной метод ───────────────────────────────────
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
        """
        Один запрос → один ответ. Без стриминга.
        Удобно для Critic/Planner, которые отдают структурированный JSON.

        Параметр `agent` (keyword-only) выбирает, какой провайдер использовать.
        """
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

        # Используем endpoint_url() — он учитывает опциональный model_url,
        # чтобы можно было переопределить полный путь для каждого агента
        # (например, отдельный NIM-деплоймент для Critic).
        url = provider.endpoint_url()

        # ── Debug-логирование: видно, КУДА реально идёт запрос ─────
        # Замаскируем api_key, чтобы он случайно не утек в логи/консоль.
        headers = self._headers(provider)
        masked_headers = {
            **headers,
            "Authorization": f"Bearer {provider.api_key[:6]}...{provider.api_key[-4:]}",
        }
        print(f"[NVIDIA->{agent.value}] POST {url}")
        print(f"[NVIDIA->{agent.value}] headers: {masked_headers}")
        print(f"[NVIDIA->{agent.value}] payload: {json.dumps(payload, ensure_ascii=False)[:1500]}")
        # ── DEBUG_HEADERS / DEBUG_PAYLOAD_MODEL: хирургическая диагностика ──
        # Прямо перед отправкой: какие именно ключи заголовков уходят
        # и какое имя модели в payload. Помогает отличить «битый заголовок»
        # от «битой модели» при 404.
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
                raise LLMError(
                    f"NVIDIA network error ({agent.value} @ {url}): {e}"
                ) from e

        if resp.status_code != 200:
            # Отдельной строкой печатаем ПОЛНЫЙ URL, по которому пошёл запрос —
            # это и есть главный диагностический сигнал при 404.
            print(
                f"[NVIDIA←{agent.value}] HTTP {resp.status_code} for URL: {url}\n"
                f"[NVIDIA←{agent.value}] response body (first 500 chars): {resp.text[:500]}"
            )
            raise LLMError(
                f"NVIDIA API {resp.status_code} ({agent.value}) @ {url}: {resp.text[:500]}"
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
class OllamaClient(BaseLLMClient):
    """
    Клиент локального Ollama.
    Docs: https://github.com/ollama/ollama/blob/main/docs/api.md

    Поддерживает как /api/chat (стриминг), так и обычный режим.
    """

    def __init__(self, base_url: str = "http://localhost:11434", default_model: Optional[str] = None):
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model or ""
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
        """Один запрос → один ответ. Для Executor (часто вызывает tools)."""
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
                # httpx.ReadTimeout / ConnectError и т.п. имеют пустой str();
                # показываем имя класса, чтобы оператор сразу видел причину.
                detail = str(e) or type(e).__name__
                raise LLMError(f"Ollama network error ({type(e).__name__}): {detail}") from e

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

# ───────────────────────────────────────────────────────────────────
# Google Gemini Client
# ───────────────────────────────────────────────────────────────────
class GoogleGeminiClient(BaseLLMClient):
    """Клиент для Google AI Studio (Gemini)."""

    def __init__(
        self,
        api_key: str,
        timeout: Optional[float] = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise LLMError("Google Gemini API key is missing")
        self.api_key = api_key.strip()
        self._timeout = timeout or settings.llm_timeout_seconds
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
        # Mapping rules
        system_instruction = None
        contents: List[Dict[str, Any]] = []

        # ── Сборка tool-ответов: Gemini требует, чтобы все
        # functionResponse-ы шли В ОДНОМ user-message, причём сразу после
        # model-сообщения, содержащего соответствующие functionCall. Идём по
        # `messages` и схлопываем подряд идущие TOOL-роли в один
        # user-блок с parts=[{functionResponse: ...}, ...].
        pending_tool_parts: List[Dict[str, Any]] = []

        def _flush_tool_parts() -> None:
            """Сбрасывает накопленные functionResponse в отдельный user-блок."""
            nonlocal pending_tool_parts
            if pending_tool_parts:
                contents.append({"role": "user", "parts": pending_tool_parts})
                pending_tool_parts = []

        for m in messages:
            if m.role == "system":
                # system messages go to systemInstruction in Gemini
                if system_instruction is None:
                    system_instruction = {"parts": [{"text": m.content}]}
                else:
                    system_instruction["parts"].append({"text": m.content})
                # Если system встретился после tool-сообщений — закрываем буфер.
                _flush_tool_parts()
            elif m.role == "assistant":
                # Сначала закроем накопленные tool-ответы.
                _flush_tool_parts()
                parts: List[Dict[str, Any]] = []
                if m.content:
                    parts.append({"text": m.content})
                if m.tool_calls:
                    for tc in m.tool_calls:
                        # Fallback parsing for tool_calls format (dict or ToolCall obj)
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
                # Tool-результат → functionResponse-часть. Gemini требует `name`,
                # поэтому тянем его из ChatMessage.name (проставляется в
                # agent-цикле при сериализации результатов tools.js-порта).
                func_name = m.name or ""
                if not func_name:
                    # Попробуем поднять имя из tool_call_id; если не вышло —
                    # логируем warning, но всё равно отправляем (с пустым name),
                    # чтобы цикл не упал.
                    log.warning(
                        "Tool message without function name (tool_call_id=%s) — "
                        "Gemini may reject. Set ChatMessage.name when serialising.",
                        m.tool_call_id,
                    )
                # `response` — это объект, который отдаётся модели. Кладём туда
                # текст (output) и явный success-флаг, чтобы LLM мог отличить
                # ошибку от штатного результата.
                response_payload: Dict[str, Any] = {
                    "output": m.content or "",
                }
                # Если content выглядит как "ERROR: ..." — прокинем отдельно.
                if isinstance(m.content, str) and m.content.startswith("ERROR:"):
                    response_payload["error"] = m.content
                pending_tool_parts.append({
                    "functionResponse": {
                        "name": func_name,
                        "response": response_payload,
                    }
                })
            else:
                # user / другие
                _flush_tool_parts()
                contents.append({"role": "user", "parts": [{"text": m.content}]})

        # Закрыть буфер tool-ответов в конце.
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
            # Поддерживаем оба формата на входе:
            #   1) OpenAI-style: [{"type": "function", "function": {name, description, parameters}}, ...]
            #   2) Уже-готовый Gemini: [{"functionDeclarations": [...]}, ...]
            # Это позволяет провайдеру принять и `mgr.openai_tools()`,
            # и `mgr.gemini_tools_payload()` без адаптеров.
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
                    # Уже плоский declaration — пропускаем как есть.
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
            # empty response case
            return ChatMessage(role="assistant", content="", tool_calls=None)

        # Parse text and function calls
        text = ""
        tool_calls = []

        for part in parts:
            if "text" in part:
                text += part["text"]
            if "functionCall" in part:
                fc = part["functionCall"]
                # For compatibility with Trinity's ToolCall format, wrap it similar to OpenAI
                args = fc.get("args", {})
                if isinstance(args, dict):
                    args_str = json.dumps(args)
                else:
                    args_str = str(args)
                
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

    def __init__(
        self,
        api_key: str,
        timeout: Optional[float] = None,
    ) -> None:
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
                            
                        # Generate a pseudo ID for anthropic's tool_use
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
                # user
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
