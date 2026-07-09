"""
agents/executor.py
──────────────────
ExecutorAgent — исполнитель.

Модель: локальный Ollama (qwen2.5-coder).
Задача: получить одобренный план и выполнить его шаг за шагом
с помощью инструментов (bash, файлы).

Toolbox-этап: Executor не только «выполняет», но и формирует для
пользователя ОСМЫСЛЕННЫЙ ФИНАЛЬНЫЙ ОТЧЁТ, опираясь на tool_outcomes —
список всех ToolResult-ов, накопленных в ctx.tool_outcomes. Это та
самая «связь write_file с ответом», которую запросил пользователь:
после успешного write_file модель отдельно формулирует, что было
создано/изменено, и пользователь видит это в SSE final-event.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from core.llm_clients import BaseLLMClient, LLMError, NvidiaClient, OllamaClient
from core.models import AgentName, ChatMessage, Role, ToolResult
from tools.registry import ToolRegistry

from agents.base import Agent, AgentContext

log = logging.getLogger("trinity.executor")


SYSTEM_PROMPT = """Ты — ExecutorAgent в мульти-агентной системе Trinity.

Твоя задача — ВЫПОЛНИТЬ план, который составил Planner и одобрил Critic.

У тебя есть инструменты:
- execute_bash(команда)  — выполнить shell-команду (PowerShell или bash)
- read_file(путь, start_line, end_line) — прочитать файл (можно указать строки для больших файлов)
- write_file(путь, содержимое) — создать или ПЕРЕЗАПИСАТЬ файл целиком
- replace_in_file(path, target_content, replacement_content) — заменить точный кусок кода в файле (предпочтительнее для изменения существующих файлов!)
- delete_file(путь)      — удалить файл
- search_in_file(путь, pattern) — найти regex в файле/директории
- list_dir(путь)         — посмотреть содержимое директории

Правила:
1. Выполняй шаги плана СТРОГО по порядку.
2. Для каждого шага вызывай соответствующий инструмент.
   Система поддерживает и нативный OpenAI tools=[] (предпочтительно),
   и блоки ```json {name, arguments}```, и <tool_call>...</tool_call>.
3. САМОИСПРАВЛЕНИЕ (SELF-CORRECTION): Если шаг провалился (например, execute_bash вернул код ошибки, линтер нашел проблемы, или replace_in_file не нашел строку), ты ОБЯЗАН проанализировать ошибку, понять причину и ВЫЗВАТЬ ИНСТРУМЕНТ СНОВА с исправленными аргументами. Не сдавайся и не завершай работу с ошибкой, пока не исчерпаешь все разумные попытки исправления!
4. Никогда не выполняй опасные команды (rm -rf /, форматирование, и т.п.).
5. Все пути должны оставаться внутри workspace_dir — иначе сработает
   SecurityError и операция будет заблокирована.
6. По завершении — напиши краткий отчёт о проделанной работе.
"""


# Лимит на размер output-а одного tool-а, попадающего в финальный
# отчёт-LLM. Без него один гигантский read_file утопит контекст.
_TOOL_OUTPUT_TRUNCATE = 500


class ExecutorAgent(Agent):
    name = AgentName.EXECUTOR
    SYSTEM_PROMPT = SYSTEM_PROMPT
    LLM_PROVIDER = "ollama"
    MODEL_NAME = "qwen2.5-coder"

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        nvidia: Optional[NvidiaClient] = None,
        ollama: Optional[OllamaClient] = None,
        llm_client: Optional[BaseLLMClient] = None,
        tools: Optional[ToolRegistry] = None,
    ):
        super().__init__(
            model=model or self.MODEL_NAME,
            nvidia=nvidia,
            ollama=ollama,
            llm_client=llm_client,
            tools=tools,
        )

    # ── helpers ─────────────────────────────────────────────────
    @staticmethod
    def _format_tool_summary(outcomes: List[ToolResult]) -> str:
        """
        Собирает читаемую человеком сводку по результатам всех tool-ов.
        Используется и для подсказки LLM, и для fallback-вывода, если
        LLM не сможет ответить.
        """
        lines: List[str] = []
        for r in outcomes:
            status = "✅" if r.success else "❌"
            out = (r.output or r.error or "").replace("\n", " ")[:_TOOL_OUTPUT_TRUNCATE]
            lines.append(f"{status} {r.name}: {out}")
        return "\n".join(lines) if lines else "(no tools executed)"

    async def _request_final_report(
        self,
        ctx: AgentContext,
        previous_content: str,
    ) -> Optional[str]:
        """
        Делает один ДОПОЛНИТЕЛЬНЫЙ LLM-вызов с просьбой сформулировать
        финальный отчёт на основе ctx.tool_outcomes.

        Зачем отдельный вызов, а не просто дописка в system prompt
        основного цикла: основной цикл в Agent.run() уже завершён к
        этому моменту (LLM либо сказала «всё готово», либо упёрлась
        в лимит итераций). Пытаемся «дожать» структурированный отчёт.

        Возвращает None, если вызов упал (тогда вызывающий код
        использует fallback).
        """
        if not ctx.tool_outcomes:
            return None
        if not self._llm_client and not self._ollama and not self._nvidia:
            return None

        summary = self._format_tool_summary(ctx.tool_outcomes)
        try:
            report_msg = await self._call_llm(
                messages=[
                    ChatMessage(role=Role.SYSTEM, content=self.SYSTEM_PROMPT),
                    ChatMessage(role=Role.USER, content=ctx.task),
                    ChatMessage(role=Role.ASSISTANT, content=previous_content or ""),
                    ChatMessage(
                        role=Role.SYSTEM,
                        content=(
                            "Сводка выполненных tool-вызовов:\n"
                            f"{summary}\n\n"
                            "Сформулируй КРАТКИЙ финальный отчёт пользователю "
                            "от первого лица: что сделано, какие файлы созданы/изменены, "
                            "ключевые пути и размеры. Без воды, без повторов исходного "
                            "ответа. На русском."
                        ),
                    ),
                ],
                temperature=0.3,
                max_tokens=1024,
                tool_schemas=None,  # финальная сводка — tool-ы уже не нужны
            )
            content = (getattr(report_msg, "content", None) or "").strip()
            return content or None
        except LLMError as e:
            log.warning("Executor final-report LLM call failed: %s", e)
            return None
        except Exception as e:  # noqa: BLE001
            log.exception("Executor final-report crashed")
            return None

    # ── основной цикл ──────────────────────────────────────────
    async def run(self, ctx: AgentContext) -> ChatMessage:
        """
        Переопределяем, чтобы:
          1) выполнить основной цикл (как у базового Agent);
          2) собрать список тронутых файлов и tool_outcomes в meta
             (для UI и для последующего аудита);
          3) если были реальные tool-вызовы — попросить LLM
             сформулировать финальный отчёт; если не вышло —
             приклеить сырую сводку к result.content.
        """
        # Гарантируем наличие поля — старые вызывающие коды могут
        # конструировать AgentContext без него.
        if not hasattr(ctx, "tool_outcomes") or ctx.tool_outcomes is None:
            ctx.tool_outcomes = []

        result = await super().run(ctx)

        # 1) Meta-данные для UI.
        result.meta["touched_files"] = list(self.tools.touched_paths)
        result.meta["tool_outcomes"] = [
            {
                "tool": r.name,
                "success": r.success,
                "output": r.output[:_TOOL_OUTPUT_TRUNCATE],
                "error": r.error,
                "duration_ms": r.duration_ms,
            }
            for r in ctx.tool_outcomes
        ]

        # 2) Если tool-ов не было — отдаём как есть, не выдумываем отчёт.
        if not ctx.tool_outcomes:
            return result

        # 3) Доп. вызов LLM для структурированного финального отчёта.
        previous = result.content
        report = await self._request_final_report(ctx, previous_content=previous)

        if report:
            # Заменяем контент: модель уже сформулировала отчёт «своими словами».
            result.content = report
            result.meta["report_summary_used_llm"] = True
        else:
            # Fallback: дописываем сырую сводку, чтобы пользователь
            # ВСЕ РАВНО видел, что tool-ы сделали.
            summary = self._format_tool_summary(ctx.tool_outcomes)
            sep = "\n\n" if previous else ""
            result.content = (
                f"{previous}{sep}— Сводка инструментов —\n{summary}"
            )
            result.meta["report_summary_used_llm"] = False

        return result
