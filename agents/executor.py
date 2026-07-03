"""
agents/executor.py
──────────────────
ExecutorAgent — исполнитель.

Модель: локальный Ollama (qwen2.5-coder).
Задача: получить одобренный план и выполнить его шаг за шагом
с помощью инструментов (bash, файлы).
"""

from __future__ import annotations

import logging
from typing import Optional

from core.llm_clients import OllamaClient
from core.models import AgentName
from tools.registry import ToolRegistry

from agents.base import Agent, AgentContext

log = logging.getLogger("trinity.executor")


SYSTEM_PROMPT = """Ты — ExecutorAgent в мульти-агентной системе Trinity.

Твоя задача — ВЫПОЛНИТЬ план, который составил Planner и одобрил Critic.

У тебя есть инструменты:
- execute_bash(команда)  — выполнить shell-команду (PowerShell или bash)
- read_file(путь)        — прочитать файл
- write_file(путь, содержимое) — создать/перезаписать файл
- list_dir(путь)         — посмотреть содержимое директории

Правила:
1. Выполняй шаги плана СТРОГО по порядку.
2. Для каждого шага вызывай соответствующий инструмент,
   оборачивая вызов в ```json ... ```.
3. Если шаг провалился — проанализируй ошибку и попробуй исправить
   (но не выходи за рамки плана).
4. Никогда не выполняй опасные команды (rm -rf /, форматирование, и т.п.).
5. По завершении — напиши краткий отчёт о проделанной работе.

Формат вызова инструмента (если провайдер не поддерживает нативный tools):
```json
{"name": "execute_bash", "arguments": {"command": "ls -la"}}
```
"""


class ExecutorAgent(Agent):
    name = AgentName.EXECUTOR
    SYSTEM_PROMPT = SYSTEM_PROMPT
    LLM_PROVIDER = "ollama"
    MODEL_NAME = "qwen2.5-coder"

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        nvidia=None,
        ollama: Optional[OllamaClient] = None,
        tools: Optional[ToolRegistry] = None,
    ):
        super().__init__(
            model=model or self.MODEL_NAME,
            nvidia=nvidia,
            ollama=ollama,
            tools=tools,
        )

    async def run(self, ctx: AgentContext):
        """
        Переопределяем, чтобы после успешного выполнения
        собрать список всех созданных/изменённых файлов.
        """
        result = await super().run(ctx)
        # Помечаем в meta, какие файлы трогали (для UI)
        result.meta["touched_files"] = list(self.tools.touched_paths)
        return result
