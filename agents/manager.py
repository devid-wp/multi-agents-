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

«Неубиваемая» версия run_task: любая ошибка (TypeError, AttributeError,
ValueError, LLMError, GeneratorExit) логируется и НИКОГДА не валит SSE-стрим.
Если Critic вернул None/пустой контент — трактуем это как «VERDICT: OK»,
чтобы цикл не зацикливался и Executor всё-таки получил шанс.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator, Dict, List, Optional

from core.config import UserCredentials, settings
from core.llm_clients import BaseLLMClient, LLMError, NvidiaClient, OllamaClient, OpenAICompatibleClient
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
        providers: Dict[AgentName, tuple] = {}
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
        # ── DEBUG_CRITIC: принудительный override для диагностики 404 ──
        # Хардкодим модель и base_url, чтобы исключить любые внешние
        # влияния (cookie-сессия, config.py, .env). Если с этими
        # значениями Critic всё равно вернёт 404 — дело в NVIDIA API
        # (rate limit / quota / доступ к модели), а не в нашем коде.
        print(
            f"DEBUG_CRITIC: hardcoded model='meta/llama-3.1-70b-instruct', "
            f"base_url='https://integrate.api.nvidia.com/v1' "
            f"(was: model={creds.critic_model!r}, "
            f"creds.critic_base_url={creds.critic_base_url!r})"
        )
        self.critic = CriticAgent(
            model="meta/llama-3.1-70b-instruct",
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
    # Главный цикл (неубиваемая версия)
    # ──────────────────────────────────────────────────────────────
    async def run_task(
        self,
        user_task: str,
    ) -> AsyncGenerator[ProgressEvent, None]:
        """
        Оркестрирует Planner → Critic ↔ Planner → Executor.
        Отдаёт наружу поток ProgressEvent для SSE.

        Гарантии устойчивости:
          • Любой LLMError / TypeError / AttributeError / ValueError,
            возникающий внутри цикла, логируется и не валит стрим.
          • Если critic.run() вернул None или пустой ChatMessage —
            трактуем это как «VERDICT: OK» и идём к Executor-у.
          • Если клиент отвалился (GeneratorExit при yield) — корректно
            завершаемся без traceback.
          • На каждом yield стоит try/except, чтобы вызовы из main.py
            (event_stream) не падали с TypeError.
        """

        def _safe_verdict(critic_response) -> str:
            """
            Извлекаем verdict из ответа Critic.
            Если ответ None / пустой / без content — возвращаем дефолт
            «VERDICT: OK» (с пометкой), чтобы цикл не зацикливался
            и Executor всё-таки получил шанс выполнить задачу.
            """
            if critic_response is None:
                log.warning("critic.run() returned None — treating as OK")
                return "VERDICT: OK (critic returned no response)"
            content = getattr(critic_response, "content", None)
            if not content or not str(content).strip():
                log.warning(
                    "critic.run() returned empty content "
                    "(response=%r) — treating as OK",
                    critic_response,
                )
                return "VERDICT: OK (critic returned empty content)"
            return str(content).strip()

        try:
            # Преамбула
            yield ProgressEvent(
                kind="info",
                agent=AgentName.MANAGER,
                content=f"🚀 Получена задача: {(user_task or '')[:200]}",
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
                try:
                    event_q.put_nowait(ev)
                except Exception as e:  # noqa: BLE001
                    log.warning("emit() failed: %s", e)
                # Глобальный канал диагностики (Live Diagnostics UI).
                # Публикуем ТОЛЬКО tool_call/tool_result/error — остальные
                # события остаются в /api/chat стриме.
                try:
                    if ev.kind in {"tool_call", "tool_result", "error"}:
                        diagnostics_bus.publish(ev)
                except Exception as e:  # noqa: BLE001
                    log.warning("diagnostics_bus.publish() failed: %s", e)

            ctx_factory = lambda task, history=None: AgentContext(  # noqa: E731
                task=task or "",
                history=history or [],
                emit=emit,
                tools=self.tools,
                max_tool_iterations=settings.max_iterations,
            )

            # ── Шаг 1: Planner пишет первый план ─────────────────
            plan_text = ""
            history: List[ChatMessage] = []
            try:
                plan_msg = await self.planner.run(ctx_factory(user_task, history=[]))
                if plan_msg is None or not getattr(plan_msg, "content", None):
                    log.warning("planner.run() returned empty/None — using stub plan")
                    plan_text = "(пустой план от Planner)"
                else:
                    plan_text = plan_msg.content
                    history.append(plan_msg)
            except LLMError as e:
                yield ProgressEvent(kind="error", agent=AgentName.MANAGER, content=str(e))
                return
            except Exception as e:  # noqa: BLE001
                log.exception("planner.run() crashed")
                yield ProgressEvent(
                    kind="error",
                    agent=AgentName.MANAGER,
                    content=f"Planner упал с неожиданной ошибкой: {e}",
                )
                return

            # ── Шаг 2: цикл Planner ↔ Critic ──────────────────────
            for i in range(settings.max_iterations):
                # ── Critic.run с тройной защитой ─────────────────
                verdict_msg = None
                try:
                    verdict_msg = await self.critic.run(
                        ctx_factory(
                            f"План от Planner:\n\n{plan_text}\n\nОцени его по критериям.",
                            history=history,
                        )
                    )
                except LLMError as e:
                    log.warning("critic LLMError (iter=%s): %s", i, e)
                    yield ProgressEvent(
                        kind="info",
                        agent=AgentName.MANAGER,
                        content=f"⚠ Critic вернул ошибку (итерация {i + 1}); считаем план OK: {e}",
                    )
                    # Трактовка «как ОК» — выходим из цикла и идём к Executor
                    break
                except (TypeError, AttributeError, ValueError) as e:
                    # Сюда попадаем, если critic.run() вернул мусор
                    # (None без .content и т.п.) и где-то выше по стеку
                    # был неудачный getattr. Не валим весь стрим.
                    log.warning(
                        "critic.run() returned malformed response (iter=%s): %s",
                        i, e,
                    )
                    yield ProgressEvent(
                        kind="info",
                        agent=AgentName.MANAGER,
                        content=f"⚠ Critic вернул неожиданный ответ (итерация {i + 1}); считаем план OK.",
                    )
                    break
                except Exception as e:  # noqa: BLE001
                    log.exception("critic.run() crashed (iter=%s)", i)
                    yield ProgressEvent(
                        kind="info",
                        agent=AgentName.MANAGER,
                        content=f"⚠ Critic упал с неожиданной ошибкой (итерация {i + 1}); считаем план OK.",
                    )
                    break

                # ── Защита от None / пустого ChatMessage ─────────
                verdict = _safe_verdict(verdict_msg)
                # Сохраняем в history только валидный объект
                if verdict_msg is not None and getattr(verdict_msg, "content", None):
                    history.append(verdict_msg)

                yield ProgressEvent(
                    kind="info",
                    agent=AgentName.MANAGER,
                    content=f"📝 Critic (итерация {i + 1}): {verdict[:200]}",
                )

                if verdict.upper().startswith("VERDICT: OK"):
                    yield ProgressEvent(
                        kind="info",
                        agent=AgentName.MANAGER,
                        content=f"✅ Critic одобрил план (итерация {i + 1}).",
                    )
                    break

                if (
                    verdict.upper().startswith("VERDICT: REVISION")
                    or i < settings.max_iterations - 1
                ):
                    # Отправляем критику обратно Planner-у
                    yield ProgressEvent(
                        kind="info",
                        agent=AgentName.MANAGER,
                        content=f"🔁 Critic запросил правки (итерация {i + 1}).",
                    )
                    try:
                        revision = await self.planner.run(
                            ctx_factory(
                                f"Critic обнаружил проблемы:\n\n{verdict}\n\n"
                                f"Исходный план:\n\n{plan_text}\n\n"
                                "Перепиши план с учётом замечаний.",
                                history=history,
                            )
                        )
                    except LLMError as e:
                        log.warning("planner revision LLMError (iter=%s): %s", i, e)
                        yield ProgressEvent(
                            kind="info",
                            agent=AgentName.MANAGER,
                            content=f"Planner не смог пересмотреть план; используем текущий: {e}",
                        )
                        break
                    except Exception as e:  # noqa: BLE001
                        log.exception("planner revision crashed (iter=%s)", i)
                        yield ProgressEvent(
                            kind="info",
                            agent=AgentName.MANAGER,
                            content="Planner упал на ревизии; используем текущий план.",
                        )
                        break

                    if revision is None or not getattr(revision, "content", None):
                        log.warning("planner revision returned empty — keeping prior plan")
                        yield ProgressEvent(
                            kind="info",
                            agent=AgentName.MANAGER,
                            content="Planner вернул пустую ревизию; используем предыдущий план.",
                        )
                        # Не обновляем plan_text — идём дальше со старым
                    else:
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
            while not event_q.empty():
                try:
                    queued = event_q.get_nowait()
                    if queued is not None:
                        yield queued
                except Exception as e:  # noqa: BLE001
                    log.warning("failed to drain event_q: %s", e)
                    break

            # ── Шаг 3: Executor выполняет одобренный план ─────────
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
            except Exception as e:  # noqa: BLE001
                log.exception("executor.run() crashed")
                yield ProgressEvent(
                    kind="error",
                    agent=AgentName.MANAGER,
                    content=f"Executor упал с неожиданной ошибкой: {e}",
                )
                return

            # Финальное событие — с защитой от None/пустого контента
            if final is None:
                log.warning("executor returned None — emitting stub final")
                yield ProgressEvent(
                    kind="final",
                    agent=AgentName.EXECUTOR,
                    content="(Executor не вернул результат)",
                )
                return
            final_content = getattr(final, "content", None) or "(пустой результат от Executor)"
            yield ProgressEvent(
                kind="final",
                agent=AgentName.EXECUTOR,
                content=final_content,
            )
        except GeneratorExit:
            # Клиент отвалился от SSE-стрима. Не шумим, не валим traceback.
            log.info("client disconnected during run_task (GeneratorExit)")
            return
        except asyncio.CancelledError:
            # Задача была отменена (например, при остановке сервера).
            log.info("run_task cancelled")
            raise
        except Exception as e:  # noqa: BLE001
            # Последний рубеж: что бы ни случилось — стрим не должен падать
            # с необработанным исключением, иначе клиент получит обрыв без
            # осмысленного сообщения.
            log.exception("unhandled exception in run_task")
            try:
                yield ProgressEvent(
                    kind="error",
                    agent=AgentName.MANAGER,
                    content=f"Внутренняя ошибка: {e}",
                )
            except Exception:  # noqa: BLE001
                # Если даже yield упал (стрим уже закрыт) — глотаем.
                pass
            return
