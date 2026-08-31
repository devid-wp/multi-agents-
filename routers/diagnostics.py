"""
routers/diagnostics.py — вынесено из main.py для Фазы 2.
"""
from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.diagnostics import diagnostics_bus

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@router.get("/stream")
async def diagnostics_stream(request: Request):
    queue = diagnostics_bus.subscribe()

    async def gen() -> AsyncGenerator[str, None]:
        try:
            yield ": ready\n\n"
            ping_at = asyncio.get_event_loop().time() + 15.0
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    now = asyncio.get_event_loop().time()
                    if now >= ping_at:
                        yield ": ping\n\n"
                        ping_at = now + 15.0
                    continue
                yield f"data: {payload}\n\n"
                ping_at = asyncio.get_event_loop().time() + 15.0
        except asyncio.CancelledError:
            return
        finally:
            diagnostics_bus.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/history")
async def diagnostics_history(limit: int = Query(200, ge=1, le=500)):
    return JSONResponse(diagnostics_bus.history(limit=limit))
