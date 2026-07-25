"""
agents/manager.py
─────────────────
AgentManager — центральный координатор Trinity.
Жёстко переориентирован на локальную Ollama (qwen2.5-coder:1.5b) для стабильности.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator, Dict, List, Optional

from core.config import UserCredentials, settings
from core.llm_clients import (
    BaseLLMClient,
    LLMError,
    NvidiaClient,
    OllamaClient,
    OpenAICompatibleClient,
    OpenRouterClient,
)
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
from core.diagnostics import diagnostics_bus
from core.history import HistoryManager

log = logging.getLogger("trinity.manager")


class AgentManager:
    """
    Создаёт и хранит инстансы агентов. Перенаправлен на локальную Олламу.
    """

    def __init__(self, creds: UserCredentials, session_id: Optional[str] = None):
        self.creds = creds
        self.session_id = session_id
        self.tools = ToolRegistry(workspace=settings.workspace_dir)
        self.history_manager = HistoryManager(workspace_dir=settings.workspace_dir)

        # Выключаем ClineToolManager, так как работаем полностью локально через ToolRegistry
        self.cline_tool_manager = None

        # ── ЖЁСТКИЙ ЛОКАЛЬНЫЙ ФОРС ОЛЛАМЫ ДЛЯ ВСЕХ АГЕНТОВ ────────────────
        local_model = "qwen2.5-coder:1.5b"
        log.info("[OLLAMA_FORCE] Инициализируем единый локальный OllamaClient для всех агентов. Модель: %s", local_model)
        
        shared_client = OllamaClient(
            base_url="http://localhost:11434", 
            default_model=local_model
        )
        
        planner_client = shared_client
        critic_client = shared_client
        executor_client = shared_client
        # ───────────────────────────────────────────────────────────────────

        self.planner = PlannerAgent(
            model=local_model,
            llm_client=planner_client,
            tools=self.tools,
        )
        self.critic = CriticAgent(
            model=local_model,
            llm_client=critic_client,
            tools=self.tools,
        )
        self.executor = ExecutorAgent(
            model=local_model,
            llm_client=executor_client,
            tools=self.tools,
        )

        # Совместимость
        self._nvidia = None
        self._ollama = shared_client
        self._openrouter = None

    # ──────────────────────────────────────────────────────────────
    # Проверка готовности
    # ──────────────────────────────────────────────────────────────
    def readiness_report(self) -> dict:
        local_model = "qwen2.5-coder:1.5b"
        return {
            "planner_configured":  True,
            "critic_configured":   True,
            "executor_configured": True,
            "executor_provider":   "ollama",
            "planner_provider":    "ollama",
            "critic_provider":     "ollama",
            "ollama_configured":   True,
            "openrouter_configured": False,
            "planner_model":    local_model,
            "critic_model":     local_model,
            "executor_model":   local_model,
            "planner_base_url": "http://localhost:11434",
            "critic_base_url":  "http://localhost:11434",
            "planner_model_url": None,
            "critic_model_url":  None,
        }

    # ──────────────────────────────────────────────────────────────
    # Главный цикл (неубиваемая версия)
    # ──────────────────────────────────────────────────────────────
    async def run_task(
        self,
        user_task: str,
        strategy: str = "auto",
    ) -> AsyncGenerator[ProgressEvent, None]:
        """
        Оркестрирует агентов согласно стратегии.
        """
        def _safe_verdict(critic_response) -> str:
            if critic_response is None:
                log.warning("critic.run() returned None — treating as OK")
                return "VERDICT: OK (critic returned no response)"
            content = getattr(critic_response, "content", None)
            if not content or not str(content).strip():
                return "VERDICT: OK (critic returned empty content)"
            return str(content).strip()
            
        session_history: List[ChatMessage] = []
        history = None
        if self.session_id:
            try:
                session_history = await asyncio.to_thread(self.history_manager.load, self.session_id)
            except Exception as e:
                log.error(f"Failed to load history for session {self.session_id}: {e}")

        try:
            effective_strategy = strategy if strategy in ("auto", "planner", "direct") else "auto"
            yield ProgressEvent(kind="strategy", agent=AgentName.MANAGER, content=effective_strategy)
            yield ProgressEvent(kind="info", agent=AgentName.MANAGER, content=f"🚀 Получена задача [{effective_strategy.upper()}]: {(user_task or '')[:200]}")

            event_q: asyncio.Queue = asyncio.Queue()

            def emit(ev: ProgressEvent) -> None:
                try:
                    event_q.put_nowait(ev)
                except Exception as e:
                    log.warning("emit() failed: %s", e)
                try:
                    if ev.kind in {"tool_call", "tool_result", "error"}:
                        diagnostics_bus.publish(ev)
                except Exception as e:
                    log.warning("diagnostics_bus.publish() failed: %s", e)

            def ctx_factory(task: str, history: List[ChatMessage]) -> AgentContext:
                return AgentContext(
                    task=task,
                    history=history,
                    emit=emit,
                    tools=self.tools,
                    max_tool_iterations=settings.max_iterations,
                    cline_tool_manager=self.cline_tool_manager,
                )

            if effective_strategy == "direct":
                yield ProgressEvent(kind="info", agent=AgentName.MANAGER, content="⚡ Режим DIRECT: передаю задачу напрямую Executor.")
                try:
                    final = await self.executor.run(ctx_factory(
                        f"Задача пользователя: {user_task}\n\nВыполни её напрямую, используя доступные инструменты.",
                        session_history
                    ))
                except Exception as e:
                    log.exception("executor.run() crashed (direct mode)")
                    yield ProgressEvent(kind="error", agent=AgentName.MANAGER, content=f"Executor упал: {e}")
                    return
                while not event_q.empty():
                    try:
                        yield event_q.get_nowait()
                    except Exception:
                        break
                final_content = getattr(final, "content", None) or "(пустой результат)"
                yield ProgressEvent(kind="final", agent=AgentName.EXECUTOR, content=final_content)
                return

            history = session_history

            # Шаг 1: Planner пишет первый план
            plan_text = ""
            try:
                plan_msg = await self.planner.run(ctx_factory(user_task, history))
                if plan_msg is None or not getattr(plan_msg, "content", None):
                    plan_text = "(пустой план от Planner)"
                else:
                    plan_text = plan_msg.content
                    history.append(plan_msg)
            except Exception as e:
                log.exception("planner.run() crashed")
                yield ProgressEvent(kind="error", agent=AgentName.MANAGER, content=f"Planner упал: {e}")
                return

            # Шаг 2: цикл Planner ↔ Critic
            for i in range(settings.max_iterations):
                verdict_msg = None
                try:
                    verdict_msg = await self.critic.run(ctx_factory(
                        f"План от Planner:\n\n{plan_text}\n\nОцени его по критериям.",
                        history=history,
                    ))
                except Exception as e:
                    log.exception("critic.run() crashed")
                    yield ProgressEvent(kind="info", agent=AgentName.MANAGER, content=f"⚠ Critic упал; считаем план OK.")
                    break

                verdict = _safe_verdict(verdict_msg)
                if verdict_msg is not None and getattr(verdict_msg, "content", None):
                    history.append(verdict_msg)

                yield ProgressEvent(kind="info", agent=AgentName.MANAGER, content=f"📝 Critic (итерация {i + 1}): {verdict[:200]}")

                if verdict.upper().startswith("VERDICT: OK"):
                    yield ProgressEvent(kind="info", agent=AgentName.MANAGER, content=f"✅ Critic одобрил план (итерация {i + 1}).")
                    break

                if i >= settings.max_iterations - 1:
                    yield ProgressEvent(kind="info", agent=AgentName.MANAGER, content=f"⚠️ Достигнут лимит итераций. Передаю план Executor-у как есть.")
                    break

                if verdict.upper().startswith("VERDICT: REVISION"):
                    yield ProgressEvent(kind="info", agent=AgentName.MANAGER, content=f"🔁 Critic запросил правки (итерация {i + 1}).")
                    try:
                        revision = await self.planner.run(ctx_factory(
                            f"Critic обнаружил проблемы:\n\n{verdict}\n\nИсходный план:\n\n{plan_text}\n\nПерепиши план с учётом замечаний.",
                            history=history,
                        ))
                    except Exception as e:
                        log.exception("planner revision crashed")
                        break

                    if revision is None or not getattr(revision, "content", None):
                        yield ProgressEvent(kind="info", agent=AgentName.MANAGER, content="Planner вернул пустую ревизию; используем предыдущий план.")
                    else:
                        history.append(revision)
                        plan_text = revision.content
                else:
                    break

            while not event_q.empty():
                try:
                    queued = event_q.get_nowait()
                    if queued is not None:
                        yield queued
                except Exception:
                    break

            if effective_strategy == "planner":
                yield ProgressEvent(kind="final", agent=AgentName.PLANNER, content=plan_text)
                return

            # Шаг 3: Executor выполняет одобренный план
            try:
                final = await self.executor.run(ctx_factory(
                    f"Одобренный план:\n\n{plan_text}\n\nИсходная задача пользователя: {user_task}\n\nВыполни его пошагово, используя инструменты.",
                    history,
                ))
            except Exception as e:
                log.exception("executor.run() crashed")
                yield ProgressEvent(kind="error", agent=AgentName.MANAGER, content=f"Executor упал: {e}")
                return

            if final is None:
                yield ProgressEvent(kind="final", agent=AgentName.EXECUTOR, content="(Executor не вернул результат)")
                return
            final_content = getattr(final, "content", None) or "(пустой результат от Executor)"
            yield ProgressEvent(kind="final", agent=AgentName.EXECUTOR, content=final_content)
        except GeneratorExit:
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("unhandled exception in run_task")
            try:
                yield ProgressEvent(kind="error", agent=AgentName.MANAGER, content=f"Внутренняя ошибка: {e}")
            except Exception:
                pass
        finally:
            if self.session_id and history:
                try:
                    await asyncio.to_thread(self.history_manager.save, self.session_id, history)
                except Exception as e:
                    log.error(f"Failed to save history for session {self.session_id}: {e}")