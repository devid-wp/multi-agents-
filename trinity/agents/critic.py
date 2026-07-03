"""
agents/critic.py
────────────────
CriticAgent — рецензент.

Модель: NVIDIA (google/gemma-2-27b-it).
Задача: получить план от Planner-а и оценить его: безопасность,
полноту, корректность. Вернуть либо «OK», либо список правок.
"""

from __future__ import annotations

from typing import Optional

from core.llm_clients import NvidiaClient, OllamaClient
from core.models import AgentName
from tools.registry import ToolRegistry

from agents.base import Agent


SYSTEM_PROMPT = """Ты — CriticAgent в мульти-агентной системе Trinity.

Твоя задача — КРИТИЧЕСКИ оценить план, который составил PlannerAgent.

Что проверять:
1. Полнота — все ли шаги покрывают задачу?
2. Корректность — нет ли логических ошибок в последовательности?
3. Безопасность — нет ли опасных команд (rm -rf, доступ за пределы
   рабочей директории, сетевые операции с непроверенными URL)?
4. Эффективность — нет ли лишних шагов, которые можно объединить?
5. Инструменты — использованы ли правильные инструменты (bash/read/write)?

Формат ответа:
- Если план OK — начни ответ со слов "VERDICT: OK", затем коротко обоснуй.
- Если нужны правки — начни со "VERDICT: REVISION", затем список
  конкретных замечаний (что исправить, в каком шаге).
Будь строгим, но конструктивным. Не предлагай альтернативных планов —
только ревью.
"""


class CriticAgent(Agent):
    name = AgentName.CRITIC
    SYSTEM_PROMPT = SYSTEM_PROMPT
    LLM_PROVIDER = "nvidia"
    MODEL_NAME = "google/gemma-2-27b-it"

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        nvidia: Optional[NvidiaClient] = None,
        ollama: Optional[OllamaClient] = None,
        tools: Optional[ToolRegistry] = None,
    ):
        super().__init__(
            model=model or self.MODEL_NAME,
            nvidia=nvidia,
            ollama=ollama,
            tools=tools,
        )
