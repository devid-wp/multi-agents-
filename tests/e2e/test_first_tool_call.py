"""
tests/e2e/test_first_tool_call.py
──────────────────────────────────
E2E-тест «первого Tool Call» Trinity.

Сценарий: мок-LLM принимает решение позвать get_system_status,
Registry исполняет инструмент, результат возвращается в историю
сообщений, и на следующей итерации мок-LLM уже выдаёт финальный
ответ пользователю — без tool-ов.

Зачем мок-LLM, а не реальный провайдер:
  • детерминизм — тест не флакает из-за сети/квот;
  • тест проверяет ЦИКЛ Registry↔Executor↔LLM, а не саму LLM;
  • один и тот же код используется любым агентом (Planner/Critic/Executor),
    так что достаточно проверить «склейку» на одном мок-клиенте.

NB: это НЕ unit-тест Registry (для него есть tests/test_tools.py).
    Это сквозной тест Agent-loop, чтобы убедиться, что tool_call →
    result → next-turn работает как задумано.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from core.models import ChatMessage, Role, ToolCall, ToolResult
from tools.registry import ToolRegistry
from tools.system_tool import GetSystemStatus


# ───────────────────────────────────────────────────────────────────
# MockLLM — «мозг» теста: заранее знает, что сказать на каждом шаге
# ───────────────────────────────────────────────────────────────────
class MockLLM:
    """
    Минимальный мок OpenAI-совместимого клиента.

    Хранит очередь «запланированных ответов». На каждом вызове chat()
    возвращает следующий. Если очередь кончилась — кидает AssertionError,
    чтобы тест сразу показал, где цикл зациклился.

    Зачем copy_from_chat вместо реальной LLM:
      Agent-loop в проде делает `client.chat(tools=..., messages=...)`.
      Нам нужно подменить именно `.chat(...)`, а не сеть — поэтому мок
      принимает те же kwargs и возвращает ChatMessage, как реальный клиент.
    """

    def __init__(self, scripted: List[ChatMessage]) -> None:
        self._scripted = list(scripted)
        self.calls: List[Dict[str, Any]] = []  # для ассертов в тесте

    async def chat(
        self,
        *,
        model: str,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        **_: Any,
    ) -> ChatMessage:
        self.calls.append({
            "model": model,
            "tools_count": len(tools) if tools else 0,
            "messages_count": len(messages),
        })
        if not self._scripted:
            raise AssertionError(
                "MockLLM: script ended, but Agent-loop still calls chat(). "
                "Это значит, что Tool Result не привёл LLM к финалу."
            )
        return self._scripted.pop(0)


# ───────────────────────────────────────────────────────────────────
# Agent-loop — упрощённая копия продового цикла «call → exec → feed back»
# ───────────────────────────────────────────────────────────────────
async def run_agent_loop(
    *,
    client: MockLLM,
    registry: ToolRegistry,
    history: List[ChatMessage],
    tools_schemas: List[Dict[str, Any]],
    max_steps: int = 5,
) -> List[ChatMessage]:
    """
    Минимальный цикл: LLM решает → исполняем tool_call → результат
    кладём обратно в историю (role='tool') → снова LLM.

    Ограничение max_steps — защита от бесконечного цикла. В проде
    AgentManager тоже имеет подобный лимит (см. agents/executor.py).

    Возвращает финальную историю сообщений (для ассертов в тесте).
    """
    for step in range(max_steps):
        response = await client.chat(
            model="mock-model",
            messages=history,
            tools=tools_schemas,
        )
        history.append(response)

        # Если tool_calls нет — это финальный ответ, цикл завершён.
        if not response.tool_calls:
            return history

        # Исполняем ВСЕ tool_calls из одного ответа (батч).
        for raw in response.tool_calls:
            # raw приходит в OpenAI-формате:
            #   {"id": "call_...", "type": "function",
            #    "function": {"name": "...", "arguments": "{...}"}}
            fn = raw.get("function") or {}
            name = fn.get("name") or raw.get("name") or ""
            args_raw = fn.get("arguments") or raw.get("arguments") or "{}"
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
            else:
                args = args_raw or {}

            call = ToolCall(id=raw.get("id", ""), name=name, arguments=args)
            result: ToolResult = await registry.execute(call)

            # Tool-результат в историю — role='tool', tool_call_id
            # обязателен (иначе OpenAI-совместимые API падают).
            history.append(
                ChatMessage(
                    role=Role.TOOL,
                    content=result.output if result.success else f"ERROR: {result.error}",
                    tool_call_id=result.tool_call_id,
                )
            )

    raise AssertionError(f"Agent-loop hit max_steps={max_steps} без финала")


# ───────────────────────────────────────────────────────────────────
# Тесты
# ───────────────────────────────────────────────────────────────────
class TestFirstToolCall:
    @pytest.mark.asyncio
    async def test_get_system_status_full_loop(self, temp_workspace: Path) -> None:
        """
        Сквозной сценарий:
          1) LLM решает позвать get_system_status;
          2) Registry исполняет, результат попадает в историю;
          3) На втором шаге LLM уже даёт финальный ответ.

        Это ИМЕННО тот цикл, который увидят Planner/Executor/Manager
        в проде — только без сети.
        """
        # Импортируем Path локально, чтобы фикстура подхватилась.
        from pathlib import Path

        # Шаг 1: LLM «решает» позвать tool.
        step1_response = ChatMessage(
            role=Role.ASSISTANT,
            content="Сначала проверю окружение.",
            tool_calls=[{
                "id": "call_001",
                "type": "function",
                "function": {
                    "name": "get_system_status",
                    "arguments": "{}",
                },
            }],
        )
        # Шаг 2: LLM «видит» результат и даёт финальный ответ.
        step2_response = ChatMessage(
            role=Role.ASSISTANT,
            content="Окружение проверено, можно продолжать.",
            tool_calls=None,
        )
        client = MockLLM(scripted=[step1_response, step2_response])

        registry = ToolRegistry(workspace=str(temp_workspace))
        schemas = registry.list_schemas()

        history: List[ChatMessage] = [
            ChatMessage(role=Role.USER, content="Проверь систему."),
        ]

        final_history = await run_agent_loop(
            client=client,
            registry=registry,
            history=history,
            tools_schemas=schemas,
        )

        # ── Структурные ассерты ─────────────────────────────────
        # 1) LLM получила schemas на КАЖДОМ шаге (важно: tool-call без
        #    схем модель не сможет вызвать корректно).
        assert client.calls[0]["tools_count"] == len(schemas)
        assert client.calls[0]["tools_count"] >= 1
        assert client.calls[1]["tools_count"] == len(schemas)

        # 2) get_system_status зарегистрирован и его схема валидна.
        names = [t["function"]["name"] for t in schemas]
        assert "get_system_status" in names

        # 3) История содержит: USER → ASSISTANT(tool_call) → TOOL → ASSISTANT(final).
        assert [m.role for m in final_history] == [
            Role.USER, Role.ASSISTANT, Role.TOOL, Role.ASSISTANT,
        ]

        # 4) Tool-результат содержит ожидаемые поля (платформа + Python).
        tool_msg = final_history[2]
        assert "platform:" in tool_msg.content
        assert "python:" in tool_msg.content
        assert tool_msg.tool_call_id == "call_001"

        # 5) Финальный ответ — без tool_calls, как ожидалось.
        final = final_history[-1]
        assert final.role == Role.ASSISTANT
        assert final.tool_calls in (None, [])
        assert "проверено" in final.content.lower()

        # 6) MockLLM «выговорил» весь скрипт ровно за 2 вызова.
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_get_system_status_unit(self, temp_workspace: Path) -> None:
        """
        Изолированный smoke-test самого tool-а (без Registry и LLM).
        Ловит регрессии в схеме и формате вывода.
        """
        from pathlib import Path

        tool = GetSystemStatus()
        # Схема должна быть OpenAI-совместимой.
        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "get_system_status"
        assert schema["function"]["parameters"]["type"] == "object"
        assert schema["function"]["parameters"]["additionalProperties"] is False

        # Пустые arguments — норма.
        result = await tool.execute({})
        assert "platform:" in result
        assert "python:" in result
        assert "utc_now:" in result

        # «Лишние» аргументы (LLM попыталась добавить своё) — игнорируются.
        result_evil = await tool.execute({"include": "everything", "path": "/etc"})
        assert "platform:" in result_evil
        # Попытки прочитать файл через этот tool не работают by design.
        assert "/etc" not in result_evil
