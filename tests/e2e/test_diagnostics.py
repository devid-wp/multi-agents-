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
from pathlib import Path
from typing import Any, Dict, Generator, List

import httpx
import pytest

from agents.base import AgentContext
from agents.executor import ExecutorAgent
from core.diagnostics import diagnostics_bus
from core.models import AgentName, ChatMessage, ProgressEvent, Role, ToolCall
from tools.registry import ToolRegistry


# ───────────────────────────────────────────────────────────────────
# Утилита: чистим singleton-буфер между тестами.
# ───────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_diagnostics_bus() -> Generator[None, None, None]:
    """
    Сбрасывает состояние глобальной шины перед каждым тестом.

    close() обнуляет _buffer и _subs. После close() новые publish()
    станут no-op (флаг _closed), поэтому пересоздаём singleton и
    подменяем ссылку в `main`, чтобы прод-импорт тоже видел свежую шину.
    """
    import core.diagnostics as diag_mod
    import main as main_mod
    import tools.registry as reg_mod

    new_bus = diag_mod.DiagnosticsBus()
    diag_mod.diagnostics_bus = new_bus
    main_mod.diagnostics_bus = new_bus
    reg_mod.diagnostics_bus = new_bus

    global diagnostics_bus
    diagnostics_bus = new_bus
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


@pytest.mark.asyncio
async def test_executor_agent_write_file_emits_tool_execution(
    temp_workspace: Path,
) -> None:
    """ExecutorAgent вручную делает write_file и публикует tool_execution."""
    history_before = diagnostics_bus.history(limit=100)

    registry = ToolRegistry(workspace=str(temp_workspace))
    executor = ExecutorAgent(tools=registry)

    responses = [
        ChatMessage(
            role=Role.ASSISTANT,
            content='{"name":"write_file","arguments":{"path":"test.txt","content":"hello"}}',
        ),
        ChatMessage(role=Role.ASSISTANT, content="OK"),
        ChatMessage(role=Role.ASSISTANT, content="Файл test.txt создан."),
    ]

    async def fake_call_llm(messages: list[ChatMessage], **kwargs: object) -> ChatMessage:
        return responses.pop(0)

    executor._call_llm = fake_call_llm  # type: ignore[assignment]

    ctx = AgentContext(
        task="create file test.txt with content hello",
        tools=registry,
        emit=lambda _: None,
    )

    result = await executor.run(ctx)

    assert (temp_workspace / "test.txt").read_text(encoding="utf-8") == "hello"
    assert "test.txt" in result.content

    history_after = diagnostics_bus.history(limit=100)
    tool_execution_events = [ev for ev in history_after if ev.get("kind") == "tool_execution"]
    assert any(ev.get("tool") == "write_file" for ev in tool_execution_events), tool_execution_events


@pytest.mark.asyncio
async def test_executor_agent_blocked_write_file_publishes_tool_execution(
    temp_workspace: Path,
) -> None:
    """ExecutorAgent пытается писать /etc/passwd — в шине появляется tool_execution."""
    registry = ToolRegistry(workspace=str(temp_workspace))
    executor = ExecutorAgent(tools=registry)

    responses = [
        ChatMessage(
            role=Role.ASSISTANT,
            content='{"name":"write_file","arguments":{"path":"/etc/passwd","content":"bad"}}',
        ),
        ChatMessage(role=Role.ASSISTANT, content="OK"),
        ChatMessage(role=Role.ASSISTANT, content="Попытка заблокирована."),
    ]

    async def fake_call_llm(messages: list[ChatMessage], **kwargs: object) -> ChatMessage:
        return responses.pop(0)

    executor._call_llm = fake_call_llm  # type: ignore[assignment]

    ctx = AgentContext(
        task="delete file /etc/passwd",
        tools=registry,
        emit=lambda _: None,
    )

    result = await executor.run(ctx)

    assert "[BLOCKED]" in result.content or "заблокирован" in result.content
    history = diagnostics_bus.history(limit=100)
    assert any(ev.get("kind") == "tool_execution" and ev.get("tool") == "write_file" for ev in history), history


# ───────────────────────────────────────────────────────────────────
# /api/diagnostics/stream  (реальный HTTP через live uvicorn)
#
# NOTE: httpx.ASGITransport дедлочится на бесконечных SSE-стримах,
# потому что gen() и aiter_lines() делят один event loop и взаимно
# блокируют друг друга. Поэтому SSE-тесты запускают uvicorn в
# фоновом потоке (фикстура live_server_url) и используют реальный HTTP.
# ───────────────────────────────────────────────────────────────────

async def test_stream_subscribes_and_emits_published_event(
    live_server_url: str,
) -> None:
    """
    Sanity fan-out через реальный HTTP:
      a) открываем SSE-стрим к live-серверу;
      b) параллельно публикуем tool_call через diagnostics_bus;
      c) читаем data-кадр — он соответствует нашему событию.
    """
    ev = ProgressEvent(
        kind="tool_call",
        agent=AgentName.PLANNER,
        tool=ToolCall(name="list_dir", arguments={"path": "."}),
    )

    async def _publish_after_delay() -> None:
        # Ждём, пока SSE-генератор на сервере сделает subscribe().
        # subscribe() вызывается ДО первого yield, т.е. до отправки `: ready`.
        # После sleep(0.5) сервер гарантированно зарегистрировал подписчика.
        await asyncio.sleep(0.5)
        diagnostics_bus.publish(ev)

    events: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=live_server_url, timeout=10.0) as client:
        async with client.stream("GET", "/api/diagnostics/stream") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")

            task = asyncio.create_task(_publish_after_delay())
            try:
                async with asyncio.timeout(6.0):
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            events.append(json.loads(line[len("data:"):].strip()))
                        except json.JSONDecodeError:
                            continue
                        if len(events) >= 1:
                            break
            except TimeoutError:
                pass
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    assert len(events) >= 1, f"Не получили ни одного SSE-кадра: {events!r}"
    first = events[0]
    assert first["kind"] == "tool_call"
    assert first["agent"] == "planner"
    assert first["tool"]["name"] == "list_dir"


async def test_stream_does_not_send_history_to_new_subscribers(
    live_server_url: str,
) -> None:
    """
    Новый клиент не должен получить события, опубликованные ДО подписки
    (шина — pub/sub, не replay).
    """
    old_ev = ProgressEvent(kind="tool_result", agent=AgentName.EXECUTOR)
    diagnostics_bus.publish(old_ev)  # до подписки — НЕ должен прийти новому клиенту

    new_ev = ProgressEvent(
        kind="tool_call",
        agent=AgentName.CRITIC,
        tool=ToolCall(name="read_file", arguments={"path": "x"}),
    )

    async def _publish_after_delay() -> None:
        await asyncio.sleep(0.5)
        diagnostics_bus.publish(new_ev)

    events: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=live_server_url, timeout=10.0) as client:
        async with client.stream("GET", "/api/diagnostics/stream") as resp:
            assert resp.status_code == 200

            task = asyncio.create_task(_publish_after_delay())
            try:
                async with asyncio.timeout(4.0):
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            events.append(json.loads(line[len("data:"):].strip()))
                        except json.JSONDecodeError:
                            continue
                        if len(events) >= 1:
                            break
            except TimeoutError:
                pass
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    assert len(events) == 1, f"Ожидался ровно 1 кадр, получили {len(events)}: {events!r}"
    assert events[0]["agent"] == "critic"
    assert events[0]["tool"]["name"] == "read_file"

