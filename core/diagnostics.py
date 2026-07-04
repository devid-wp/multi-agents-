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
from typing import Any, Deque, Dict, List, Set

from core.models import ProgressEvent

log = logging.getLogger("trinity.diagnostics")

# ───────────────────────────────────────────────────────────────────
# Какие события пропускаем в шину. Остальные (agent_start, agent_done,
# agent_message, final, info) остаются только в /api/chat стриме.
# ───────────────────────────────────────────────────────────────────
DIAGNOSTIC_KINDS: frozenset[str] = frozenset({"tool_call", "tool_result", "error"})

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
        self._lock = asyncio.Lock()
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

    # ── подписка ────────────────────────────────────────────────
    async def subscribe(self) -> asyncio.Queue[str]:
        """Регистрирует нового подписчика и возвращает его очередь."""
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAXSIZE)
        async with self._lock:
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
    async def close(self) -> None:
        """Закрывает шину. Дальнейшие publish() — no-op."""
        async with self._lock:
            self._closed = True
            self._subs.clear()
            self._buffer.clear()


# ───────────────────────────────────────────────────────────────────
# Синглтон
# ───────────────────────────────────────────────────────────────────
diagnostics_bus = DiagnosticsBus()
