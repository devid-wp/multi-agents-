"""
agents/base.py
──────────────
Базовый класс для всех агентов Trinity.

Каждый агент имеет:
  • системный промпт (persona + правила)
  • клиент LLM (NVIDIA или Ollama)
  • список доступных инструментов
  • метод run(), который получает задачу и контекст,
    возвращает финальный ответ + лог событий
"""

from __future__ import annotations

import abc
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.llm_clients import BaseLLMClient, LLMError, NvidiaClient, OllamaClient
from core.models import (
    AgentName,
    ChatMessage,
    ProgressEvent,
    Role,
    ToolCall,
    ToolResult,
)
from tools.registry import ToolRegistry

log = logging.getLogger("trinity.agents")


# ───────────────────────────────────────────────────────────────────
# Контекст, передаваемый в run()
# ───────────────────────────────────────────────────────────────────
@dataclass
class AgentContext:
    """Общее состояние для одного прогона агента."""

    task: str
    history: List[ChatMessage] = field(default_factory=list)
    # Callback, через который агент «вещает» в SSE-стрим
    emit: Optional[Any] = None  # Callable[[ProgressEvent], None]
    # Ссылка на глобальный реестр инструментов
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    # Сколько итераций tool-calling разрешено
    max_tool_iterations: int = 5
    # Накапливается во время Agent._run_tools — каждый ToolResult,
    # выполненный в рамках этого прогона. Executor использует для
    # финального отчёта; Planner/Critic игнорируют.
    tool_outcomes: List[ToolResult] = field(default_factory=list)


# ───────────────────────────────────────────────────────────────────
# Абстрактный агент
# ───────────────────────────────────────────────────────────────────
class Agent(abc.ABC):
    """
    Базовый класс. Конкретные агенты (Planner/Critic/Executor) переопределяют
    SYSTEM_PROMPT, LLM_PROVIDER и MODEL.
    """

    name: AgentName
    SYSTEM_PROMPT: str = ""
    LLM_PROVIDER: str = "nvidia"  # "nvidia" | "ollama"
    MODEL_NAME: str = ""

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        nvidia: Optional[NvidiaClient] = None,
        ollama: Optional[OllamaClient] = None,
        llm_client: Optional[BaseLLMClient] = None,
        tools: Optional[ToolRegistry] = None,
    ):
        self.MODEL_NAME = model or self.MODEL_NAME
        self._nvidia = nvidia
        self._ollama = ollama
        self._llm_client = llm_client
        self.tools = tools or ToolRegistry()

    # ── helpers ──────────────────────────────────────────────────────
    def _emit(self, ctx: AgentContext, event: ProgressEvent) -> None:
        """Прокидывает событие в SSE-стрим, если callback задан."""
        if ctx.emit:
            try:
                ctx.emit(event)
            except Exception as e:  # noqa: BLE001
                log.warning("emit failed: %s", e)

    async def _call_llm(
        self,
        messages: List[ChatMessage],
        *,
        temperature: float = 0.6,
        max_tokens: int = 2048,
        tool_schemas: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatMessage:
        """Универсальный вызов LLM (nvidia или ollama)."""
        # ── DEBUG_TOOLS_CALL: какие схемы уходят в LLM для этого агента ──
        # Critic принципиально не должен получать tools (он только ревьюит план),
        # а gemma-2-27b-it и некоторые другие модели NVIDIA NIM возвращают 404
        # или 400, если в payload есть секция `tools`, которую они не поддерживают.
        # На Planner tools нужны; на Critic — нет. Жёстко отфильтруем здесь.
        if self.name == AgentName.CRITIC:
            if tool_schemas:
                print(
                    f"DEBUG_TOOLS_CALL: {self.name.value} — "
                    f"stripping {len(tool_schemas)} tool schema(s) "
                    f"(critic must not request tools)"
                )
            tool_schemas = None

        if self._llm_client is not None:
            return await self._llm_client.chat(
                model=self.MODEL_NAME,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tool_schemas,
                agent=self.name,
            )
        if self.LLM_PROVIDER == "nvidia":
            if not self._nvidia:
                raise LLMError(f"{self.name}: NVIDIA client not configured")
            return await self._nvidia.chat(
                model=self.MODEL_NAME,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tool_schemas,
                agent=self.name,  # ← выбираем ключ/URL для конкретного агента
            )
        if self.LLM_PROVIDER == "ollama":
            if not self._ollama:
                raise LLMError(f"{self.name}: Ollama client not configured")
            return await self._ollama.chat(
                model=self.MODEL_NAME,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tool_schemas,
            )
        raise LLMError(f"Unknown LLM_PROVIDER: {self.LLM_PROVIDER}")

    # ── tool-call parsing (fallback, если провайдер не поддерживает tools) ──
    @staticmethod
    def parse_json_tool_calls(content: str) -> List[ToolCall]:
        """
        Парсит tool-вызовы из текстового ответа LLM.

        Поддерживает три формата (порядок приоритета — сверху вниз):
          1. ```json {name, arguments}```   — Cline-style, то, что Ollama
             (qwen2.5-coder) обычно эмитит.
          2. <tool_call>{...}</tool_call>       — Hermes / OpenAI tool-call
             xml-style. Многие NVIDIA NIM-модели используют именно его.
          3. tool_call_id / function_call / bare JSON — единичные крайние
             случаи (например, llama-3.1 любит «action: read_file(...)»).

        Если ничего не найдено — возвращает [].
        При наличии нативного response.tool_calls (OpenAI-стиль) — этот
        метод НЕ вызывается, нативный путь имеет приоритет.
        """
        calls: List[ToolCall] = []

        # ── 1) Cline-style ```json ... ``` ─────────────────────────
        for match in re.finditer(r"```json\s*(\{.*?\}|\[.*?\])\s*```", content, re.DOTALL):
            try:
                obj = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(obj, list):
                items = obj
            else:
                items = [obj]
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("tool")
                args = item.get("arguments") or item.get("args") or item.get("input") or {}
                if name:
                    calls.append(ToolCall(name=name, arguments=args))

        # Если уже нашли Cline-блок — обычно этого достаточно. Но бывают
        # модели, которые мешают форматы; пройдёмся и по <tool_call>, чтобы
        # не терять вызовы, если первый парсер дал пусто.
        if calls:
            return calls

        # ── 2) <tool_call>...</tool_call> (Hermes / OpenAI xml) ──────
        for match in re.finditer(
            r"<tool_call>\s*(\{.*?\})\s*</tool_call>", content, re.DOTALL
        ):
            try:
                obj = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            name = obj.get("name") or obj.get("tool")
            # Hermes любит вложенный {"arguments": "{...json string...}"}
            args = obj.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            elif not isinstance(args, dict):
                args = obj.get("args") or obj.get("input") or {}
            if name:
                calls.append(ToolCall(name=name, arguments=args or {}))

        if calls:
            return calls

        # ── 3) Крайний случай: одиночный JSON-объект с name/tool ──
        # Не матчим "любой JSON", иначе проглотим тело ответа.
        # Ограничиваемся одним объектом, в котором есть name/tool.
        stripped = content.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                name = obj.get("name") or obj.get("tool")
                args = obj.get("arguments") or obj.get("args") or obj.get("input") or {}
                if name:
                    calls.append(ToolCall(name=name, arguments=args or {}))

        return calls

    async def _run_tools(
        self,
        ctx: AgentContext,
        tool_calls: List[ToolCall],
    ) -> List[ToolResult]:
        """
        Выполняет tool-calls и эмитит события.

        Дополнительно накапливает результаты в ctx.tool_outcomes — это
        «боковая» лента для Executor-а, чтобы он мог сформировать
        финальный отчёт на основе реальных tool-результатов.
        """
        results: List[ToolResult] = []
        for call in tool_calls:
            self._emit(ctx, ProgressEvent(kind="tool_call", agent=self.name, tool=call))
            result = await self.tools.execute(call, workspace=ctx.tools.workspace)
            self._emit(ctx, ProgressEvent(kind="tool_result", agent=self.name, result=result))
            # Накапливаем для post-processing (Executor → отчёт)
            try:
                ctx.tool_outcomes.append(result)
            except AttributeError:
                # Если ctx был создан из чужой версии AgentContext (без
                # поля tool_outcomes) — не валим выполнение, просто не
                # копим. Свежие ctx-ы это поле имеют.
                pass
            results.append(result)
        return results

    # ── основной цикл ──────────────────────────────────────────────
    async def run(self, ctx: AgentContext) -> ChatMessage:
        """
        Главный метод. Делает:
          1. Системный промпт + история.
          2. Вызов LLM.
          3. Если есть tool-calls — выполняет, добавляет в историю, повторяет.
          4. Возвращает финальный ответ.
        """
        # Стартовое событие
        self._emit(ctx, ProgressEvent(
            kind="agent_start",
            agent=self.name,
            content=f"[{self.name.value}] думаю...",
        ))

        # Формируем начальный список сообщений
        messages: List[ChatMessage] = [ChatMessage(role=Role.SYSTEM, content=self.SYSTEM_PROMPT)]
        messages.extend(ctx.history)
        messages.append(ChatMessage(role=Role.USER, content=ctx.task, agent=self.name))

        # Схемы инструментов (если есть)
        tool_schemas = self.tools.list_schemas() if self.tools else None

        final: Optional[ChatMessage] = None
        for iteration in range(ctx.max_tool_iterations):
            try:
                response = await self._call_llm(
                    messages,
                    temperature=0.6 if self.name == AgentName.PLANNER else 0.3,
                    max_tokens=2048,
                    tool_schemas=tool_schemas,
                )
            except LLMError as e:
                self._emit(ctx, ProgressEvent(kind="error", agent=self.name, content=str(e)))
                raise

            response.agent = self.name
            messages.append(response)
            self._emit(ctx, ProgressEvent(
                kind="agent_message",
                agent=self.name,
                content=response.content,
            ))

            # Собираем tool-calls (либо нативные, либо распарсенные из JSON)
            native_calls = response.tool_calls or []
            if native_calls:
                tc_objs = [
                    ToolCall(
                        id=c.get("id") or ToolCall(id=str(i)).id,
                        name=c["function"]["name"],
                        arguments=json.loads(c["function"]["arguments"])
                        if isinstance(c["function"].get("arguments"), str)
                        else c["function"].get("arguments", {}),
                    )
                    for i, c in enumerate(native_calls)
                ]
            else:
                tc_objs = self.parse_json_tool_calls(response.content)

            if not tc_objs:
                # Нет инструментов — это финальный ответ
                final = response
                break

            # Выполняем и накапливаем результаты
            results = await self._run_tools(ctx, tc_objs)
            for call, res in zip(tc_objs, results):
                messages.append(ChatMessage(
                    role=Role.TOOL,
                    content=res.output if res.success else f"ERROR: {res.error}",
                    tool_call_id=call.id,
                ))
        else:
            # Цикл завершился без «без-tool» ответа — берём последний
            final = messages[-1] if messages else ChatMessage(
                role=Role.ASSISTANT, content="(no response)", agent=self.name
            )

        self._emit(ctx, ProgressEvent(
            kind="agent_done",
            agent=self.name,
            content=final.content if final else "",
        ))
        return final or ChatMessage(role=Role.ASSISTANT, content="", agent=self.name)
