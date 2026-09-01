"""routers/chat.py — POST /api/chat SSE + GET /api/chat/history (вынесено из main.py 0.6.0)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from core.config import UserCredentials, settings
from core.models import AgentName, ChatRequest, ProgressEvent
from core.rooms import RoomStore
from core.session import get_credentials

log = logging.getLogger("trinity.chat")

router = APIRouter(tags=["chat"])

# Rate-limit state (sliding window 60s per IP)
_chat_rate: dict[str, deque[float]] = defaultdict(deque)


def _check_rate_limit(ip: str) -> None:
    now = time.time()
    window = _chat_rate[ip]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= settings.chat_rate_limit_per_minute:
        raise HTTPException(status_code=429, detail=f"Rate limit: {settings.chat_rate_limit_per_minute}/min")
    window.append(now)


def _room_store() -> RoomStore:
    return RoomStore(settings.workspace_dir)


@router.get("/api/chat/history")
async def get_history(session_id: str = Query(""), room_id: str = Query("general")):
    from core.history import HistoryManager

    if not session_id:
        return {"ok": False, "messages": [], "error": "session_id is required"}
    hm = HistoryManager(workspace_dir=settings.workspace_dir)
    if not _room_store().exists(room_id):
        raise HTTPException(status_code=404, detail="room not found")
    scoped_session_id = RoomStore.session_id(session_id, room_id)
    messages = hm.load(scoped_session_id)
    return {"ok": True, "session_id": session_id, "room_id": room_id, "messages": [m.model_dump(mode="json") for m in messages]}


@router.post("/api/chat")
async def chat(request: Request, payload: ChatRequest):
    ip = request.client.host if request.client else "unknown"
    _check_rate_limit(ip)
    t0 = time.time()
    creds = get_credentials(request)

    if payload.ephemeral_credentials:
        ep = payload.ephemeral_credentials
        from core.models import AgentProviderConfig

        def _merge(old, new):
            if not new:
                return old
            return AgentProviderConfig(
                provider=new.provider or (old.provider if old else "nvidia"),
                api_key=new.api_key if new.api_key not in (None, "") else (old.api_key if old else None),
                base_url=new.base_url or (old.base_url if old else None),
                model_name=new.model_name or (old.model_name if old else None),
            )

        creds = UserCredentials(
            planner=_merge(creds.planner, ep.planner),
            critic=_merge(creds.critic, ep.critic),
            executor=_merge(creds.executor, ep.executor),
        )

    from agents.manager import AgentManager

    if not _room_store().exists(payload.room_id):
        raise HTTPException(status_code=404, detail="room not found")
    scoped_session_id = RoomStore.session_id(payload.session_id or "anonymous", payload.room_id)
    manager = AgentManager(creds=creds, session_id=scoped_session_id)

    async def event_stream() -> AsyncGenerator[str, None]:
        ready = manager.readiness_report()
        yield ProgressEvent(
            kind="info",
            content=(
                f"Конфигурация: Planner={'✓' if ready['planner_configured'] else '✗'}, "
                f"Critic={'✓' if ready['critic_configured'] else '✗'}, "
                f"Ollama={'✓' if ready['ollama_configured'] else '✗'}; "
                f"Planner={ready['planner_model']}, "
                f"Critic={ready['critic_model']}, "
                f"Executor={ready['executor_model']}"
            ),
        ).to_sse()
        start = t0
        try:
            gen = manager.run_task(payload.message, strategy=payload.strategy or "auto")
            while True:
                try:
                    ev = await gen.__anext__()
                except StopAsyncIteration:
                    break
                if ev is None:
                    continue
                sse_payload = getattr(ev, "to_sse", None)
                if not callable(sse_payload):
                    log.warning("event has no to_sse() — skipping: %r", ev)
                    continue
                yield sse_payload()
        except asyncio.CancelledError:
            log.info("client disconnected (CancelledError) mid-stream")
            try:
                await gen.aclose()
            except Exception:
                pass
            return
        except GeneratorExit:
            log.info("client disconnected (GeneratorExit) mid-stream")
            return
        except Exception as e:
            log.exception("unhandled error in run_task")
            try:
                yield ProgressEvent(kind="error", content=f"Unhandled error: {e}").to_sse()
            except Exception:
                pass
        finally:
            dt = time.time() - start
            est_tokens = len(payload.message) // 4
            log.info("chat done ip=%s room=%s strat=%s dt=%.2fs est_tokens=%d", ip, payload.room_id, payload.strategy or "auto", dt, est_tokens)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
