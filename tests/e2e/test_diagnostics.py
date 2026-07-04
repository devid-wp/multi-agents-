"""
tests/e2e/test_diagnostics.py
─────────────────────────────
GET /api/diagnostics/history   — последние N событий.
GET /api/diagnostics/stream    — long-lived SSE-фанаут от diagnostics_bus.

Тонкости:
  • diagnostics_bus — глобальный singleton. Между тестами в буфере могут
    накопиться события. Перед каждым тестом чистим его вручную.
  • history сериализуется в JSON-формате ProgressEvent (model_dump_json).
  • stream-эндпоинт сразу шлёт `: ready\\n\\n`, потом — кадры
    `data: <payload>\\n\\n` на каждое опубликованное событие.
  • На каждый `subscribe()` шина выдаёт ОТДЕЛЬНУЮ asyncio.Queue, и
    `unsubscribe()` отписывает именно её.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import httpx
import pytest

from core.diagnostics import diagnostics_bus
from core.models import AgentName, ProgressEvent, ToolCall


# ───────────────────────────────────────────────────────────────────
# Утилита: чистим singleton-буфер между тестами.
# ───────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_diagnostics_bus() -> None:
    """
    Сбрасывает состояние глобальной шины перед каждым тестом.

    close() обнуляет _buffer и _subs. После close() новые publish()
    станут no-op (флаг _closed), поэтому пересоздаём singleton и
    подменяем ссылку в `main`, чтобы прод-импорт тоже видел свежую шину.
    """
    import core.diagnostics as diag_mod
    import main as main_mod

    new_bus = diag_mod.DiagnosticsBus()
    diag_mod.diagnostics_bus = new_bus
    main_mod.diagnostics_bus = new_bus
    yield


# ───────────────────────────────────────────────────────────────────
# /api/diagnostics/history
# ───────────────────────────────────────────────────────────────────
async def test_history_empty_on_fresh_process(app_client: httpx.AsyncClient) -> None:
    """Свежий процесс → history пустой."""
    r = await app_client.get("/api/diagnostics/history", params={"limit": 10})
    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_history_contains_published_event(
    app_client: httpx.AsyncClient,
) -> None:
    """После publish() событие попадает в history (newest-first)."""
    ev = ProgressEvent(
        kind="tool_call",
        agent=AgentName.EXECUTOR,
        tool=ToolCall(name="write_file", arguments={"path": "a.txt"}),
    )
    diagnostics_bus.publish(ev)

    r = await app_client.get("/api/diagnostics/history", params={"limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    item = body[0]
    assert item["kind"] == "tool_call"
    assert item["agent"] == "executor"
    # ToolCall сериализуется в dict
    assert item["tool"]["name"] == "write_file"


async def test_history_filters_non_diagnostic_kinds(
    app_client: httpx.AsyncClient,
) -> None:
    """Только kinds ∈ {tool_call, tool_result, error} проходят фильтр шины."""
    diagnostics_bus.publish(ProgressEvent(kind="agent_start", agent=AgentName.MANAGER, content="x"))
    diagnostics_bus.publish(ProgressEvent(kind="final", agent=AgentName.EXECUTOR, content="x"))
    diagnostics_bus.publish(ProgressEvent(kind="error", agent=AgentName.MANAGER, content="oops"))

    r = await app_client.get("/api/diagnostics/history", params={"limit": 50})
    body = r.json()
    kinds = [ev["kind"] for ev in body]
    # Только error прошёл; agent_start и final — НЕ диагностические kinds
    assert "error" in kinds
    assert "agent_start" not in kinds
    assert "final" not in kinds


async def test_history_limit_validation(app_client: httpx.AsyncClient) -> None:
    """Pydantic-валидация: limit<1 и limit>500 → 422."""
    r0 = await app_client.get("/api/diagnostics/history", params={"limit": 0})
    assert r0.status_code == 422
    r1000 = await app_client.get("/api/diagnostics/history", params={"limit": 1000})
    assert r1000.status_code == 422


# ───────────────────────────────────────────────────────────────────
# /api/diagnostics/stream
# ───────────────────────────────────────────────────────────────────
async def _read_sse_events(
    resp: httpx.Response, *, until: int = 1, timeout: float = 5.0
) -> List[Dict[str, Any]]:
    """
    Читает SSE-кадры из `resp.aiter_lines()` и возвращает список
    распарсенных JSON-объектов из `data: …` строк.

    Останавливается, как только набрали `until` событий, или по таймауту.
    """
    events: List[Dict[str, Any]] = []

    async def _iter():
        async for line in resp.aiter_lines():
            yield line

    async with asyncio.timeout(timeout):
        async for line in _iter():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            events.append(ev)
            if len(events) >= until:
                return events
    return events


async def test_stream_subscribes_and_emits_published_event(
    app_client: httpx.AsyncClient,
) -> None:
    """
    Sanity fan-out:
      a) открываем SSE-стрим;
      b) публикуем tool_call event;
      c) читаем следующий data-кадр — он соответствует нашему событию.
    """
    ev = ProgressEvent(
        kind="tool_call",
        agent=AgentName.PLANNER,
        tool=ToolCall(name="list_dir", arguments={"path": "."}),
    )

    async with app_client.stream("GET", "/api/diagnostics/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        # Даём серверу долю секунды, чтобы он успел зарегистрировать подписчика,
        # иначе publish() доедет до пустого _subs и тест не увидит события.
        await asyncio.sleep(0.2)
        diagnostics_bus.publish(ev)

        events = await _read_sse_events(resp, until=1, timeout=5.0)

    assert len(events) >= 1, "Не получили ни одного SSE-кадра"
    # Первый data-кадр — наш tool_call
    first = events[0]
    assert first["kind"] == "tool_call"
    assert first["agent"] == "planner"
    assert first["tool"]["name"] == "list_dir"


async def test_stream_does_not_send_history_to_new_subscribers(
    app_client: httpx.AsyncClient,
) -> None:
    """
    После publish() и отписки — новый клиент не должен получить
    ретроспективно старые события (шина — pub/sub, не replay).
    """
    # Сначала публикуем событие (старый подписчик его получит, но никто не слушает)
    old_ev = ProgressEvent(
        kind="tool_result",
        agent=AgentName.EXECUTOR,
    )
    diagnostics_bus.publish(old_ev)
    # Оно уже в буфере, но НЕ должно приехать новому подписчику как live-кадр.

    async with app_client.stream("GET", "/api/diagnostics/stream") as resp:
        assert resp.status_code == 200

        # Подождём чуть-чуть и опубликуем НОВОЕ событие.
        await asyncio.sleep(0.2)
        new_ev = ProgressEvent(
            kind="tool_call",
            agent=AgentName.CRITIC,
            tool=ToolCall(name="read_file", arguments={"path": "x"}),
        )
        diagnostics_bus.publish(new_ev)

        events = await _read_sse_events(resp, until=1, timeout=3.0)

    # В стриме должен быть ТОЛЬКО новый event (старый пришёл ДО подписки).
    assert len(events) == 1, f"Ожидался ровно 1 кадр, получили {len(events)}: {events!r}"
    assert events[0]["agent"] == "critic"
    assert events[0]["tool"]["name"] == "read_file"
