"""
tests/e2e/test_bus_direct.py
────────────────────────────
Прямые unit-тесты DiagnosticsBus без HTTP-слоя.
"""
from __future__ import annotations

import asyncio

import pytest

from core.diagnostics import DiagnosticsBus
from core.models import AgentName, ProgressEvent, ToolCall


async def test_bus_subscribe_and_publish() -> None:
    """Подписчик получает событие через очередь."""
    bus = DiagnosticsBus()
    q = bus.subscribe()
    ev = ProgressEvent(
        kind="tool_call",
        agent=AgentName.PLANNER,
        tool=ToolCall(name="list_dir", arguments={"path": "."}),
    )
    bus.publish(ev)

    payload = await asyncio.wait_for(q.get(), timeout=1.0)
    import json
    data = json.loads(payload)
    assert data["kind"] == "tool_call"
    assert data["agent"] == "planner"
    bus.close()


async def test_bus_no_replay_for_new_subscriber() -> None:
    """Новый подписчик не получает события, опубликованные до подписки."""
    bus = DiagnosticsBus()

    old_ev = ProgressEvent(kind="tool_result", agent=AgentName.EXECUTOR)
    bus.publish(old_ev)  # до подписки

    q = bus.subscribe()

    new_ev = ProgressEvent(
        kind="tool_call",
        agent=AgentName.CRITIC,
        tool=ToolCall(name="read_file", arguments={"path": "x"}),
    )
    bus.publish(new_ev)  # после подписки

    import json
    payload = await asyncio.wait_for(q.get(), timeout=1.0)
    data = json.loads(payload)
    # Должен быть только новый event
    assert data["agent"] == "critic"
    assert data["tool"]["name"] == "read_file"

    # Очередь пуста — старый event не попал
    assert q.empty(), "Старый event не должен был попасть в очередь нового подписчика"
    bus.close()


async def test_bus_close_clears_subs() -> None:
    """После close() publish ничего не делает."""
    bus = DiagnosticsBus()
    q = bus.subscribe()
    bus.close()

    ev = ProgressEvent(kind="error", agent=AgentName.MANAGER, content="oops")
    bus.publish(ev)  # должен быть no-op

    assert q.empty()
