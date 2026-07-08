"""
tests/e2e/test_chat_sse.py
──────────────────────────
Главный E2E: POST /api/chat с мок-LLM (respx) и чтением SSE-стрима.

Покрывает:
  • happy path: Planner→Critic→Executor с моками (respx).
  • первый кадр — readiness report (kind: "info");
  • финальный кадр — kind: "final", agent: "executor", content: "DONE".
  • ошибка Planner (401) → kind: "error" в стриме, не 5xx на эндпоинт.
  • real-API smoke — skip, если нет PLANNER_API_KEY/CRITIC_API_KEY.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List

import httpx
import pytest
import respx

from core.config import DEFAULT_NVIDIA_URL, DEFAULT_OLLAMA_URL


# ───────────────────────────────────────────────────────────────────
# Утилиты: mock-LLM
# ───────────────────────────────────────────────────────────────────
# План и финал — минимально валидные ответы. Агенту достаточно
# непустого content; tool-calls не используются.
_PLAN_TEXT = "1. step one\n2. step two"
_VERDICT_OK = "VERDICT: OK — looks reasonable"
_FINAL_TEXT = "DONE"


def _make_nvidia_response(content: str) -> Dict[str, Any]:
    """OpenAI-совместимый ответ для NVIDIA NIM."""
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _make_ollama_response(content: str) -> Dict[str, Any]:
    """Ollama /api/chat (не-стриминг) формат."""
    return {
        "model": "qwen2.5-coder",
        "message": {"role": "assistant", "content": content},
        "done": True,
    }


def _llm_side_effect(request: httpx.Request) -> httpx.Response:
    """
    Роутер, который смотрит на тело запроса и возвращает правильный
    «ответ» в зависимости от того, КТО (Planner/Critic/Executor) спрашивает.

    Planner в manager.py использует модель из creds (по умолчанию
    abacusai/dracarys-llama-3.1-70b-instruct).
    Critic жёстко захардкожен на meta/llama-3.1-70b-instruct в manager.py.
    Ollama использует qwen2.5-coder.
    """
    try:
        payload = json.loads(request.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    model = payload.get("model") or ""

    # URL Ollama
    if "11434" in str(request.url):
        return httpx.Response(200, json=_make_ollama_response(_FINAL_TEXT))

    # NVIDIA: различаем по модели
    if "dracarys" in model:
        return httpx.Response(200, json=_make_nvidia_response(_PLAN_TEXT))
    if "llama-3.1-70b" in model:
        return httpx.Response(200, json=_make_nvidia_response(_VERDICT_OK))
    # Fallback — пусть вернёт что-то осмысленное, чтобы не 5хх
    return httpx.Response(200, json=_make_nvidia_response("OK"))


def _planner_401_side_effect(request: httpx.Request) -> httpx.Response:
    """Симулируем 401 на запросах Planner (dracarys)."""
    try:
        payload = json.loads(request.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    model = payload.get("model") or ""
    if "dracarys" in model:
        return httpx.Response(401, json={"error": "unauthorized"})
    if "llama-3.1-70b" in model:
        return httpx.Response(200, json=_make_nvidia_response(_VERDICT_OK))
    if "11434" in str(request.url):
        return httpx.Response(200, json=_make_ollama_response(_FINAL_TEXT))
    return httpx.Response(200, json=_make_nvidia_response("OK"))


# ───────────────────────────────────────────────────────────────────
# SSE-reader
# ───────────────────────────────────────────────────────────────────
async def _read_sse_events(
    resp: httpx.Response,
    *,
    stop_on: str | None = "final",
    timeout: float = 15.0,
) -> List[Dict[str, Any]]:
    """
    Читает SSE-кадры из httpx-стрима.

    Возвращает список распарсенных JSON из `data: …` строк. Пропускает
    комментарии (`: ready`, `: ping`).

    Если `stop_on` задан, останавливается, как только встретится
    событие с kind == stop_on. Это позволяет не ждать «вечно».
    """
    events: List[Dict[str, Any]] = []

    async def _iter():
        async for line in resp.aiter_lines():
            yield line

    try:
        async with asyncio.timeout(timeout):
            async for line in _iter():
                if not line.startswith("data:"):
                    # это либо ': ready', либо ': ping' — пропускаем
                    continue
                payload = line[len("data:"):].strip()
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                events.append(ev)
                if stop_on is not None and ev.get("kind") == stop_on:
                    return events
    except (asyncio.TimeoutError, TimeoutError):
        # Возвращаем то, что накопили — пусть тест сам решит, хватило ли
        return events
    return events


# ───────────────────────────────────────────────────────────────────
# Happy path
# ───────────────────────────────────────────────────────────────────
async def test_chat_happy_path_sse(app_client: httpx.AsyncClient) -> None:
    """
    Полный E2E POST /api/chat:
      1) первый кадр — kind: "info" (readiness report);
      2) есть kind: "final" с agent: "executor" и content: "DONE".
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{DEFAULT_NVIDIA_URL}/chat/completions").mock(side_effect=_llm_side_effect)
        mock.post(f"{DEFAULT_OLLAMA_URL}/api/chat").mock(side_effect=_llm_side_effect)

        body = {
            "message": "привет",
            "ephemeral_credentials": {
                "planner_api_key": "nvapi-TEST-PLANNER-1234567890ab",
                "planner_base_url": DEFAULT_NVIDIA_URL,
                "critic_api_key": "nvapi-TEST-CRITIC-1234567890ab",
                "critic_base_url": DEFAULT_NVIDIA_URL,
                "ollama_url": DEFAULT_OLLAMA_URL,
            },
        }
        async with app_client.stream("POST", "/api/chat", json=body) as resp:
            assert resp.status_code == 200, await resp.aread()
            assert resp.headers["content-type"].startswith("text/event-stream")

            events = await _read_sse_events(resp, stop_on="final", timeout=10.0)

    # Проверки
    assert events, "SSE-стрим вернул 0 событий"
    # 1) Первый data-кадр — readiness report (kind: "info")
    assert events[0]["kind"] == "info", f"first event kind: {events[0].get('kind')!r}"
    # 2) Среди событий есть финальное от Executor
    finals = [e for e in events if e.get("kind") == "final"]
    assert finals, f"не нашли kind=final среди {len(events)} событий"
    final = finals[-1]
    assert final["agent"] == "executor"
    assert final["content"] == _FINAL_TEXT


async def test_chat_planner_401_emits_error(app_client: httpx.AsyncClient) -> None:
    """
    Если Planner возвращает 401 — в стриме должен появиться kind: "error",
    эндпоинт НЕ должен вернуть 5xx (стрим закрывается нормально после error).
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{DEFAULT_NVIDIA_URL}/chat/completions").mock(
            side_effect=_planner_401_side_effect
        )
        mock.post(f"{DEFAULT_OLLAMA_URL}/api/chat").mock(side_effect=_llm_side_effect)

        body = {
            "message": "ping",
            "ephemeral_credentials": {
                "planner_api_key": "nvapi-BAD-KEY",
                "planner_base_url": DEFAULT_NVIDIA_URL,
                "critic_api_key": "nvapi-TEST-CRITIC-1234567890ab",
                "critic_base_url": DEFAULT_NVIDIA_URL,
                "ollama_url": DEFAULT_OLLAMA_URL,
            },
        }
        async with app_client.stream("POST", "/api/chat", json=body) as resp:
            # Эндпоинт остаётся 200 (SSE) даже при ошибке LLM —
            # ошибка приходит в стриме как data: {kind: "error", ...}
            assert resp.status_code == 200, await resp.aread()
            # Читаем до первого error или до таймаута
            events = await _read_sse_events(resp, stop_on="error", timeout=10.0)

    assert any(e.get("kind") == "error" for e in events), (
        f"Ожидался kind=error, события: {events!r}"
    )
    # И финала не должно быть (Planner упал — до Executor не дошли)
    assert not any(e.get("kind") == "final" for e in events), (
        "Не должно быть kind=final, если Planner упал с 401"
    )


# ───────────────────────────────────────────────────────────────────
# Real-API smoke (skip, если ключей нет)
# ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_chat_real_api_smoke(
    app_client: httpx.AsyncClient, nvidia_keys: dict
) -> None:
    """
    Smoke с реальным NVIDIA. Короткий message, чтобы быстро получить ответ.
    Skip, если PLANNER_API_KEY / CRITIC_API_KEY не заданы.

    Намеренно НЕ мокаем внешние API — тест должен сам дойти до
    `kind: "final"` или `kind: "error"`. Таймаут 60с.
    """
    body = {
        "message": "ping",
        "ephemeral_credentials": {
            "planner_api_key": nvidia_keys["planner_api_key"],
            "planner_base_url": nvidia_keys["planner_base_url"],
            "critic_api_key": nvidia_keys["critic_api_key"],
            "critic_base_url": nvidia_keys["critic_base_url"],
            # Ollama не обязателен, но передаём дефолт — если он есть
            "ollama_url": os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
        },
    }
    async with app_client.stream("POST", "/api/chat", json=body) as resp:
        assert resp.status_code == 200
        events = await _read_sse_events(resp, stop_on=None, timeout=60.0)

    # Должен быть хотя бы один из {final, error}
    assert any(e.get("kind") in ("final", "error") for e in events), (
        f"real-API прогон не пришёл ни к final, ни к error за 60с. "
        f"События: {events[:5]!r}…"
    )


@pytest.mark.asyncio
async def test_chat_real_ollama_smoke(app_client: httpx.AsyncClient) -> None:
    """
    Smoke с реальным Ollama (стратегия «direct» — только Executor).
    Skip, если OLLAMA_URL не задан или Ollama недоступна.

    Проверяет полный путь: HTTP POST → SSE-стрим → kind:final/error.
    Не мокаем ничего — это настоящий end-to-end с локальной моделью.
    Таймаут 90с (Ollama может быть медленнее, чем NVIDIA).
    """
    ollama_url = os.environ.get("OLLAMA_URL", "")
    if not ollama_url:
        pytest.skip("OLLAMA_URL не задан — пропускаем Ollama real-API тест")

    # Быстрая pre-flight проверка: Ollama вообще отвечает?
    try:
        async with httpx.AsyncClient(timeout=5.0) as probe:
            r = await probe.get(f"{ollama_url}/api/tags")
            if r.status_code != 200:
                pytest.skip(f"Ollama недоступна ({ollama_url}): HTTP {r.status_code}")
            models = r.json().get("models", [])
            if not models:
                pytest.skip(f"Ollama доступна, но моделей нет. Запусти: ollama pull qwen2.5-coder:7b")
    except Exception as e:
        pytest.skip(f"Ollama недоступна ({ollama_url}): {e}")

    # Передаём Ollama как Executor в стратегии «direct» (минимальный путь)
    body = {
        "message": "Reply with exactly: PONG",
        "strategy": "direct",
        "ephemeral_credentials": {
            # Planner и Critic не нужны при strategy=direct, но
            # AgentManager требует хотя бы executor
            "planner": {
                "provider": "ollama",
                "base_url": ollama_url,
            },
            "critic": {
                "provider": "ollama",
                "base_url": ollama_url,
            },
            "executor": {
                "provider": "ollama",
                "base_url": ollama_url,
            },
        },
    }

    async with app_client.stream("POST", "/api/chat", json=body) as resp:
        assert resp.status_code == 200, f"HTTP {resp.status_code}"
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = await _read_sse_events(resp, stop_on=None, timeout=90.0)

    assert events, "SSE-стрим вернул 0 событий"

    terminal_kinds = {e.get("kind") for e in events}
    assert terminal_kinds & {"final", "error"}, (
        f"Ollama real-flow не пришёл ни к final, ни к error за 90с. "
        f"Виды событий: {sorted(terminal_kinds)!r}, "
        f"Первые 3: {events[:3]!r}"
    )

