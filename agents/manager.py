"""
agents/manager.py
─────────────────
AgentManager — центральный координатор Trinity.

Сценарий работы:
    1. Получает user task.
    2. Запускает Planner → получает план.
    3. Передаёт план Critic → получает verdict.
    4. Если verdict == REVISION и лимит итераций не исчерпан →
       возвращает план Planner-у на доработку.
    5. Когда план одобрен — запускает Executor.
    6. Возвращает финальный результат пользователю.

Координатор транслирует все этапы в SSE-стрим, чтобы фронтенд
видел «кто сейчас говорит».
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator, List, Optional

from core.config import UserCredentials, settings
from core.llm_clients import LLMError, NvidiaClient, OllamaClient
from core.models import (
    AgentName,
    ChatMessage,
    ProgressEvent,
    Role,
)
from tools.registry import ToolRegistry

from agents.base import AgentContext
from agents.critic import CriticAgent
from agents.executor import ExecutorAgent
from agents.planner import PlannerAgent

log = logging.getLogger("trinity.manager")


class AgentManager:
    """
    Создаёт и хранит инстансы агентов, согласованные с кредентиалами
    пользователя. Один AgentManager = одна пользовательская сессия.
    """

    def __init__(self, creds: UserCredentials):
        self.creds = creds
        self.tools = ToolRegistry(workspace=settings.workspace_dir)

        # ── LLM-клиенты ───────────────────────────────────────────
        # NvidiaClient маршрутизирует запросы по AgentName:
        # Planner идёт к (planner_api_key, planner_base_url[, planner_model_url]),
        # Critic — к (critic_api_key, critic_base_url[, critic_model_url]).
        # 3-й элемент кортежа (model_url) — опциональный полный URL,
        # перекрывает {base_url}/chat/completions, если у агента свой NIM-эндпоинт.
        self._nvidia: Optional[NvidiaClient] = None
        providers: Dict = {}
        if creds.has_planner_key():
            providers[AgentName.PLANNER] = (
                creds.planner_api_key or "",
                creds.planner_base_url,
                creds.planner_model_url,
            )
        if creds.has_critic_key():
            providers[AgentName.CRITIC] = (
                creds.critic_api_key or "",
                creds.critic_base_url,
                creds.critic_model_url,
            )
        if providers:
            self._nvidia = NvidiaClient(providers=providers)

        self._ollama: Optional[OllamaClient] = None
        if creds.has_ollama():
            self._ollama = OllamaClient(base_url=creds.ollama_url)

        # ── Агенты ────────────────────────────────────────────────
        # Planner/Critic получают ОДИН И ТОТ ЖЕ NvidiaClient —
        # он сам разберётся, чей ключ использовать, по self.name.
        self.planner = PlannerAgent(
            model=creds.planner_model,
            nvidia=self._nvidia,
            tools=self.tools,
        )
        self.critic = CriticAgent(
            model=creds.critic_model,
            nvidia=self._nvidia,
            tools=self.tools,
        )
        self.executor = ExecutorAgent(
            model=creds.executor_model,
            ollama=self._ollama,
            tools=self.tools,
        )

    # ──────────────────────────────────────────────────────────────
    # Проверка готовности
    # ──────────────────────────────────────────────────────────────
    def readiness_report(self) -> dict:
        """Возвращает, какие компоненты готовы к работе."""
        return {
            "planner_configured": self.creds.has_planner_key(),
            "critic_configured": self.creds.has_critic_key(),
            "ollama_configured": self._ollama is not None,
            "planner_model": self.planner.MODEL_NAME,
            "critic_model": self.critic.MODEL_NAME,
            "executor_model": self.executor.MODEL_NAME,
            "planner_base_url": self.creds.planner_base_url,
            "critic_base_url": self.creds.critic_base_url,
            "planner_model_url": self.creds.planner_model_url,
            "critic_model_url": self.creds.critic_model_url,
        }

    # ──────────────────────────────────────────────────────────────
    # Главный цикл
    # ──────────────────────────────────────────────────────────────
    async def run_task(
        self,
        user_task: str,
    ) -> AsyncGenerator[ProgressEvent, None]:
        """
        Оркестрирует Planner → Critic ↔ Planner → Executor.
        Отдаёт наружу поток ProgressEvent для SSE.
        """
        # Преамбула
        yield ProgressEvent(
            kind="info",
            agent=AgentName.MANAGER,
            content=f"🚀 Получена задача: {user_task[:200]}",
        )

        if not self._nvidia and not self._ollama:
            yield ProgressEvent(
                kind="error",
                agent=AgentName.MANAGER,
                content="Не сконфигурирован ни один LLM-провайдер. Заполните форму.",
            )
            return

        # Очередь событий (push из корутин agent.run() в main-цикл)
        event_q: asyncio.Queue = asyncio.Queue()

        def emit(ev: ProgressEvent) -> None:
            event_q.put_nowait(ev)

        ctx_factory = lambda task, history=None: AgentContext(  # noqa: E731
            task=task,
            history=history or [],
            emit=emit,
            tools=self.tools,
            max_tool_iterations=settings.max_iterations,
        )

        # ── Шаг 1: Planner пишет первый план ──────────────────────
        plan_text = ""
        history: List[ChatMessage] = []
        try:
            plan_msg = await self.planner.run(ctx_factory(user_task, history=[]))
            plan_text = plan_msg.content
            history.append(plan_msg)
        except LLMError as e:
            yield ProgressEvent(kind="error", agent=AgentName.MANAGER, content=str(e))
            return

        # ── Шаг 2: цикл Planner ↔ Critic ──────────────────────────
        for i in range(settings.max_iterations):
            verdict_msg = await self.critic.run(
                ctx_factory(
                    f"План от Planner:\n\n{plan_text}\n\nОцени его по критериям.",
                    history=history,
                )
            )
            history.append(verdict_msg)
            verdict = verdict_msg.content.strip()

            if verdict.upper().startswith("VERDICT: OK"):
                yield ProgressEvent(
                    kind="info",
                    agent=AgentName.MANAGER,
                    content=f"✅ Critic одобрил план (итерация {i + 1}).",
                )
                break

            if verdict.upper().startswith("VERDICT: REVISION") or i < settings.max_iterations - 1:
                # Отправляем критику обратно Planner-у
                yield ProgressEvent(
                    kind="info",
                    agent=AgentName.MANAGER,
                    content=f"🔁 Critic запросил правки (итерация {i + 1}).",
                )
                revision = await self.planner.run(
                    ctx_factory(
                        f"Critic обнаружил проблемы:\n\n{verdict}\n\n"
                        f"Исходный план:\n\n{plan_text}\n\n"
                        "Перепиши план с учётом замечаний.",
                        history=history,
                    )
                )
                history.append(revision)
                plan_text = revision.content
            else:
                yield ProgressEvent(
                    kind="info",
                    agent=AgentName.MANAGER,
                    content="⚠️ Достигнут лимит итераций. Передаю план Executor-у как есть.",
                )
                break

        # ── Сливаем все события, накопившиеся во время agent.run() ─
        # (на случай, если эмитты буферизовались)
        while not event_q.empty():
            yield event_q.get_nowait()

        # ── Шаг 3: Executor выполняет одобренный план ─────────────
        try:
            final = await self.executor.run(
                ctx_factory(
                    f"Одобренный план:\n\n{plan_text}\n\n"
                    f"Исходная задача пользователя: {user_task}\n\n"
                    "Выполни его пошагово, используя доступные инструменты.",
                    history=[],
                )
            )
        except LLMError as e:
            yield ProgressEvent(kind="error", agent=AgentName.MANAGER, content=str(e))
            return

        # Финальное событие
        yield ProgressEvent(
            kind="final",
            agent=AgentName.EXECUTOR,
            content=final.content,
        )
