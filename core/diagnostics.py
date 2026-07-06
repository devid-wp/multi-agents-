"""
core/diagnostics.py
───────────────────
Глобальная шина диагностических событий Trinity.

Идея: AgentManager.run_task() эмитит ProgressEvent в свой локальный
event_q (для SSE-стрима /api/chat). Здесь мы делаем *дополнительный*
pub/sub-канал, чтобы в Live Diagnostics UI сыпались ВСЕ tool_call /
tool_result / error события со ВСЕХ параллельных прогонов — независимо
от того, открыт ли у клиента /api/chat.

Используется:
  * agents/manager.py   — publish(ev) в emit()-closure
  * main.py             — /api/diagnostics/{stream,history}
                          (читают из буфера / подписываются на шину)

Хранение: bounded deque (FIFO, maxlen=history_max). Потокобезопасно
в пределах одного event-loop (asyncio.Lock на подписчиках).
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from typing import Any, Deque, Dict, List, Optional, Set

from core.models import ProgressEvent

log = logging.getLogger("trinity.diagnostics")

# ───────────────────────────────────────────────────────────────────
# Какие события пропускаем в шину. Остальные (agent_start, agent_done,
# agent_message, final, info) остаются только в /api/chat стриме.
# ───────────────────────────────────────────────────────────────────
#   • tool_call / tool_result / error — это ProgressEvent, идущие через publish(ev).
#   • tool_execution — отдельный канал для низкоуровневого tool_execute()
#     из tools/registry.py; попадает в Live Log Stream сразу при старте
#     tool-а, до того как мы знаем результат.
DIAGNOSTIC_KINDS: frozenset[str] = frozenset({
    "tool_call", "tool_result", "error", "tool_execution",
})

# Размер очереди на одного подписчика. Если клиент не успевает
# вычитывать — дропаем самые старые события (best-effort).
SUBSCRIBER_QUEUE_MAXSIZE: int = 200

# Сколько последних событий хранить в кольцевом буфере для /history.
DEFAULT_HISTORY_MAX: int = 500


class DiagnosticsBus:
    """
    In-process pub/sub + ring buffer.

    publish() — синхронный (вызывается из emit-closure во время
    обработки очередного yield run_task-а, в event-loop потоке).
    subscribe() возвращает asyncio.Queue, из которой читает SSE-эндпоинт.
    """

    def __init__(self, *, history_max: int = DEFAULT_HISTORY_MAX) -> None:
        self._history_max = history_max
        self._buffer: Deque[str] = collections.deque(maxlen=history_max)
        self._subs: Set[asyncio.Queue[str]] = set()
        self._closed = False

    # ── публикация ──────────────────────────────────────────────
    def publish(self, ev: ProgressEvent) -> None:
        """
        Кладёт событие в буфер и фанит-аутит подписчикам.

        Только kinds ∈ DIAGNOSTIC_KINDS проходят фильтр.
        Сериализуем один раз (model_dump_json) — и в буфер, и подписчикам
        отдаём уже строку, чтобы они не делали повторный dump.
        """
        if ev.kind not in DIAGNOSTIC_KINDS:
            return
        try:
            payload = ev.model_dump_json()
        except Exception as e:  # noqa: BLE001
            log.warning("diagnostics_bus: failed to serialise event: %s", e)
            return

        # Ring buffer (FIFO; deque(maxlen=...) сама отрезает хвост)
        self._buffer.append(payload)

        # Fan-out: итерируем копию, чтобы подписчики могли
        # отписаться прямо во время publish без мутации set-а.
        for q in list(self._subs):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Медленный клиент — дропаем самый старый элемент
                # и пробуем ещё раз. Если и это не вышло — пропускаем.
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    # ── низкоуровневый канал tool_execution ─────────────────────
    def publish_tool_execution(
        self,
        *,
        tool: str,
        args: Dict[str, Any],
        call_id: str,
        agent: Optional[str] = None,
    ) -> None:
        """
        Публикует факт ВЫЗОВА tool-а (на входе в ToolRegistry.execute).

        Зачем отдельный метод, а не ProgressEvent:
          • tool_call (ProgressEvent) публикуется ПОСЛЕ успешного парсинга
            LLM-ответа и ПЕРЕД invoke. tool_execution — это маркер
            «мы реально полезли в код инструмента», включая заблокированные
            sandbox-ом вызовы, которые иначе потерялись бы.
          • payload-формат отдельный: kind='tool_execution', tool, args,
            call_id, agent, timestamp. Подходит и для Live Log Stream,
            и для последующего аудита («какие пути LLM пыталась открыть»).

        Гарантии:
          • Без блокировок — вызывается из async-цикла Agent.run() в event-loop.
          • Исключения внутри сериализации или fan-out ГЛОТАЕМ: сломанная
            шина не должна валить выполнение tool-а.
          • Дубликаты в SSE-стриме невозможны: подписчики /api/diagnostics
            читают И tool_call, И tool_execution. UI решает, что рисовать.
        """
        try:
            payload = json.dumps(
                {
                    "kind": "tool_execution",
                    "tool": tool,
                    "args": args or {},
                    "call_id": call_id,
                    "agent": agent,
                    "timestamp": time.time(),
                },
                ensure_ascii=False,
                default=str,  # Path, set и т.п. → str, иначе TypeError
            )
        except Exception as e:  # noqa: BLE001
            log.warning("diagnostics_bus.publish_tool_execution: serialise failed: %s", e)
            return

        # Ring buffer (тот же deque, что и для ProgressEvent)
        self._buffer.append(payload)

        # Fan-out (тот же код, что и в publish(), но копипастить — опасно
        # рассинхронизировать. Если subscribe() в будущем изменится —
        # обновлять оба места.)
        for q in list(self._subs):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    # ── подписка ────────────────────────────────────────────────
    def subscribe(self) -> asyncio.Queue[str]:
        """Регистрирует нового подписчика и возвращает его очередь."""
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAXSIZE)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        """Снимает подписку. Безопасно вызывать несколько раз."""
        self._subs.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    # ── история ─────────────────────────────────────────────────
    def history(self, limit: int = 200) -> List[Dict[str, Any]]:
        """
        Снимок последних событий (newest-first).

        limit ограничен снаружи (в эндпоинте); здесь режем по фактической
        длине буфера.
        """
        # deque хранит FIFO; reversed() даёт newest-first
        items = list(reversed(self._buffer))
        if limit and len(items) > limit:
            items = items[:limit]
        out: List[Dict[str, Any]] = []
        for raw in items:
            try:
                out.append(json.loads(raw))
            except (ValueError, TypeError):
                continue
        return out

    # ── lifecycle (для тестов) ──────────────────────────────────
    def close(self) -> None:
        """Закрывает шину. Дальнейшие publish() — no-op."""
        self._closed = True
        self._subs.clear()
        self._buffer.clear()


# ───────────────────────────────────────────────────────────────────
# Синглтон
# ───────────────────────────────────────────────────────────────────
diagnostics_bus = DiagnosticsBus()
