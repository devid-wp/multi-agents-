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


class LLMError(RuntimeError):
    """Любая ошибка взаимодействия с LLM-провайдером."""


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

    # Маркер «хвоста», который клиент дописывает к base_url.
    # Если base_url уже заканчивается на него — не дублируем.
    _CHAT_COMPLETIONS_SUFFIX = "/chat/completions"

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_key.strip():
            raise LLMError("NvidiaProvider: api_key is empty")
        self.api_key = self.api_key.strip()
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
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── chat() — основной метод ───────────────────────────────────
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
