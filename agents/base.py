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
    # Опциональный Cline-style tool manager (см. `trinity.tools.manager`).
    # Если задан — Agent маршрутизирует вызовы 6 «cline-инструментов»
    # (read_files / search_codebase / run_commands / fetch_web_content /
    # editor / apply_patch) через него, а не через `tools`. Это нужно для
    # провайдеров, которые передают tool-calls в формате functionCall
    # (Gemini и т.п.) и не понимают Cline-style нативный формат.
    cline_tool_manager: Optional[Any] = None


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

    def _pick_tool_schemas(self, ctx: AgentContext) -> Optional[List[Dict[str, Any]]]:
        """
        Выбирает, какой набор схем отправить в LLM.

        Приоритет:
          1. Если у контекста есть `cline_tool_manager` — отдаём его
             OpenAI-style schemas (Cline-порт 6 инструментов). Это нужно
             для провайдеров типа Gemini, которые отдают functionCall.
          2. Иначе — старые `ctx.tools.list_schemas()` (Cline-style
             execute_bash / read_file / write_file / …).
          3. None, если ни того, ни другого нет.
        """
        mgr = getattr(ctx, "cline_tool_manager", None)
        if mgr is not None:
            try:
                schemas = mgr.openai_tools()
                if schemas:
                    return schemas
            except Exception as e:  # noqa: BLE001
                log.warning("cline_tool_manager.openai_tools() failed: %s", e)
        if self.tools is not None:
            try:
                return self.tools.list_schemas()
            except Exception as e:  # noqa: BLE001
                log.warning("ToolRegistry.list_schemas() failed: %s", e)
        return None

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

        Routing:
          1. Если у `ctx.cline_tool_manager` есть этот tool → идём через
             Python-порт `trinity.tools.executors`. Это основной путь для
             Gemini-провайдеров (functionCall-формат).
          2. Иначе — старый путь через `ctx.tools` (ToolRegistry с Cline-style
             набором execute_bash / read_file / write_file / …).

        Дополнительно накапливает результаты в ctx.tool_outcomes — это
        «боковая» лента для Executor-а, чтобы он мог сформировать
        финальный отчёт на основе реальных tool-результатов.
        """
        results: List[ToolResult] = []
        mgr = getattr(ctx, "cline_tool_manager", None)
        for call in tool_calls:
            self._emit(ctx, ProgressEvent(kind="tool_call", agent=self.name, tool=call))
            result = await self._dispatch_tool(ctx, call, mgr=mgr)
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

    async def _dispatch_tool(
        self,
        ctx: AgentContext,
        call: ToolCall,
        *,
        mgr: Optional[Any],
    ) -> ToolResult:
        """
        Маршрутизация ОДНОГО tool-call-а в нужный backend.

        - Если `mgr` задан И его список 6 имён покрывает `call.name` —
          вызываем Python-executor из `trinity.tools.executors.dispatch`
          и оборачиваем результат в ToolResult. Имя функции прокинем
          в ToolResult.name, чтобы Gemini получил functionResponse.name.
        - Иначе — старый путь через `ctx.tools.execute(...)`.
        """
        cline_names: Optional[List[str]] = None
        if mgr is not None:
            try:
                cline_names = mgr.list_tool_names()
            except Exception:  # noqa: BLE001
                cline_names = None
        if mgr is not None and cline_names and call.name in cline_names:
            try:
                cline_results = await mgr.run(call.name, call.arguments or {})
            except Exception as e:  # noqa: BLE001
                log.exception("cline_tool_manager.run failed for %s", call.name)
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    success=False,
                    output="",
                    error=f"{type(e).__name__}: {e}",
                    duration_ms=0,
                )
            # Берём первый результат (для single-input tools вроде
            # editor/apply_patch) либо склеиваем всё в один текст для
            # batch-input tools (read_files/search_codebase/run_commands).
            if not cline_results:
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    success=True,
                    output="(empty result)",
                    error=None,
                    duration_ms=0,
                )
            success = all(r.success for r in cline_results)
            errors = [r for r in cline_results if r.error]
            if errors:
                error_text = "\n".join(
                    f"{r.query}: {r.error}" for r in errors
                )
            else:
                error_text = None
            if len(cline_results) == 1:
                r = cline_results[0]
                body = r.result if r.success else (r.error or "")
                if isinstance(body, dict):
                    body = json.dumps(body, ensure_ascii=False)
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    success=success,
                    output=str(body) if body is not None else "",
                    error=error_text,
                    duration_ms=0,
                )
            # multiple — format uniformly
            text = mgr.format_tool_response(call.name, cline_results) \
                if hasattr(mgr, "format_tool_response") else \
                "\n\n".join(
                    (r.result if r.success else (r.error or "")) for r in cline_results
                )
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                success=success,
                output=text,
                error=error_text,
                duration_ms=0,
            )
        return await self.tools.execute(call, workspace=ctx.tools.workspace)

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
        tool_schemas = self._pick_tool_schemas(ctx)

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
                # Нормализуем нативный формат:
                # • NVIDIA NIM / OpenAI: arguments — JSON-строка
                # • Ollama: arguments — уже dict
                # Оба случая приводим к ToolCall(name, arguments: dict)
                tc_objs = []
                for i, c in enumerate(native_calls):
                    if not isinstance(c, dict):
                        log.warning("native tool_call is not a dict: %r — skipping", c)
                        continue
                    fn = c.get("function") or {}
                    name = fn.get("name") or c.get("name")
                    if not name:
                        log.warning("native tool_call has no name: %r — skipping", c)
                        continue
                    raw_args = fn.get("arguments") or c.get("arguments") or {}
                    if isinstance(raw_args, str):
                        try:
                            args = json.loads(raw_args)
                        except json.JSONDecodeError:
                            log.warning(
                                "tool_call '%s': could not parse arguments JSON %r",
                                name, raw_args,
                            )
                            args = {"raw": raw_args}
                    else:
                        args = raw_args if isinstance(raw_args, dict) else {}
                    tc_objs.append(ToolCall(
                        id=c.get("id") or f"call_{i}",
                        name=name,
                        arguments=args,
                    ))
            else:
                tc_objs = self.parse_json_tool_calls(response.content)

            if not tc_objs:
                # Нет инструментов — это финальный ответ
                final = response
                break

            # Выполняем и накапливаем результаты
            results = await self._run_tools(ctx, tc_objs)
            for call, res in zip(tc_objs, results):
                # `name` нужен Gemini, чтобы сформировать functionResponse
                # с правильным полем `name` (требование API).
                messages.append(ChatMessage(
                    role=Role.TOOL,
                    content=res.output if res.success else f"ERROR: {res.error}",
                    tool_call_id=call.id,
                    name=res.name or call.name,
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
