"""
main.py
───────
FastAPI entry-point для Trinity Multi-Agent System.

Эндпоинты:
  GET  /                  — главная страница (чат + модалка настроек)
  GET  /api/settings      — текущие настройки пользователя (ключи маскируются)
  POST /api/settings      — сохранить настройки в сессии
  POST /api/chat          — отправить задачу, получить SSE-стрим
  GET  /api/health        — healthcheck

Запуск:
    pip install -r requirements.txt
    uvicorn main:app --reload
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.config import DEFAULT_NVIDIA_URL, UserCredentials, settings
from core.models import (
    ChatRequest,
    ProgressEvent,
    SettingsPayload,
    SettingsResponse,
)
from core.session import get_credentials, mask_key, save_credentials

# Делаем корень проекта доступным для импорта `core`, `agents`, `tools`
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("trinity.app")


# ───────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────
def _validate_url(value: Optional[str]) -> Optional[str]:
    """
    Простейшая валидация URL — должна быть http(s)://...
    Пустое значение разрешаем (значит «не менять»).
    """
    if value is None or value.strip() == "":
        return None
    v = value.strip()
    if not (v.startswith("http://") or v.startswith("https://")):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid URL: {v!r}. Must start with http:// or https://",
        )
    return v.rstrip("/") or v


# ───────────────────────────────────────────────────────────────────
# Lifespan (startup / shutdown)
# ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 Trinity starting. workspace=%s", os.path.abspath(settings.workspace_dir))
    yield
    log.info("🛑 Trinity shutting down.")


# ───────────────────────────────────────────────────────────────────
# Приложение
# ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Trinity — Multi-Agent System",
    version="0.2.0",
    lifespan=lifespan,
)

# Статика и шаблоны
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# ───────────────────────────────────────────────────────────────────
# Главная страница
# ───────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Рендерит ChatGPT-подобный интерфейс."""
    creds = get_credentials(request)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "default_nvidia_url": DEFAULT_NVIDIA_URL,
            "default_ollama_url": creds.ollama_url or "http://localhost:11434",
            "default_planner_model": creds.planner_model,
            "default_critic_model": creds.critic_model,
            "default_executor_model": creds.executor_model,
        },
    )


# ───────────────────────────────────────────────────────────────────
# Settings API
# ───────────────────────────────────────────────────────────────────
@app.get("/api/settings", response_model=SettingsResponse)
async def read_settings(request: Request):
    creds = get_credentials(request)
    return SettingsResponse(
        has_planner_key=creds.has_planner_key(),
        has_critic_key=creds.has_critic_key(),
        planner_key_masked=mask_key(creds.planner_api_key),
        critic_key_masked=mask_key(creds.critic_api_key),
        planner_base_url=creds.planner_base_url,
        critic_base_url=creds.critic_base_url,
        planner_model_url=creds.planner_model_url,
        critic_model_url=creds.critic_model_url,
        ollama_url=creds.ollama_url,
        planner_model=creds.planner_model,
        critic_model=creds.critic_model,
        executor_model=creds.executor_model,
    )


@app.post("/api/settings")
async def write_settings(request: Request, payload: SettingsPayload):
    """Сохраняет настройки в подписанной cookie-сессии."""
    current = get_credentials(request)

    # Валидация URL
    planner_url = _validate_url(payload.planner_base_url)
    critic_url = _validate_url(payload.critic_base_url)
    ollama_url = _validate_url(payload.ollama_url)
    planner_model_url = _validate_url(payload.planner_model_url)
    critic_model_url = _validate_url(payload.critic_model_url)

    # Мержим: для ключей/base URL "пусто = оставить прежнее".
    # Для model_url "пусто = сбросить переопределение" (вернуть на дефолт),
    # потому что пользователь может осознанно хотеть отключить кастомный URL.
    new = UserCredentials(
        planner_api_key=(
            payload.planner_api_key
            if payload.planner_api_key not in (None, "")
            else current.planner_api_key
        ),
        planner_base_url=planner_url or current.planner_base_url,
        planner_model_url=(
            planner_model_url
            if payload.planner_model_url is not None
            else current.planner_model_url
        ),
        critic_api_key=(
            payload.critic_api_key
            if payload.critic_api_key not in (None, "")
            else current.critic_api_key
        ),
        critic_base_url=critic_url or current.critic_base_url,
        critic_model_url=(
            critic_model_url
            if payload.critic_model_url is not None
            else current.critic_model_url
        ),
        ollama_url=ollama_url or current.ollama_url,
        planner_model=payload.planner_model or current.planner_model,
        critic_model=payload.critic_model or current.critic_model,
        executor_model=payload.executor_model or current.executor_model,
    )
    signed = save_credentials(new)
    resp = JSONResponse(
        {
            "ok": True,
            "has_planner_key": new.has_planner_key(),
            "has_critic_key": new.has_critic_key(),
            "planner_key_masked": mask_key(new.planner_api_key),
            "critic_key_masked": mask_key(new.critic_api_key),
            "planner_base_url": new.planner_base_url,
            "critic_base_url": new.critic_base_url,
            "planner_model_url": new.planner_model_url,
            "critic_model_url": new.critic_model_url,
            "ollama_url": new.ollama_url,
        }
    )
    resp.set_cookie(
        key="trinity_session",
        value=signed,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,  # 30 дней
    )
    return resp


# ───────────────────────────────────────────────────────────────────
# Healthcheck
# ───────────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {"ok": True, "service": "trinity"}


# ───────────────────────────────────────────────────────────────────
# Chat (SSE-стрим) — протокол не меняем
# ───────────────────────────────────────────────────────────────────
@app.post("/api/chat")
async def chat(request: Request, payload: ChatRequest):
    """
    Принимает задачу → возвращает SSE-поток с событиями прогресса.
    Формат события: data: <JSON ProgressEvent>\\n\\n
    """
    creds = get_credentials(request)

    # Если прислали эфемерные кредентиалы (без сохранения в сессию) —
    # используем их для этого одного запроса
    if payload.ephemeral_credentials:
        ep = payload.ephemeral_credentials
        creds = UserCredentials(
            planner_api_key=ep.planner_api_key or creds.planner_api_key,
            planner_base_url=ep.planner_base_url or creds.planner_base_url,
            planner_model_url=ep.planner_model_url or creds.planner_model_url,
            critic_api_key=ep.critic_api_key or creds.critic_api_key,
            critic_base_url=ep.critic_base_url or creds.critic_base_url,
            critic_model_url=ep.critic_model_url or creds.critic_model_url,
            ollama_url=ep.ollama_url or creds.ollama_url,
            planner_model=ep.planner_model or creds.planner_model,
            critic_model=ep.critic_model or creds.critic_model,
            executor_model=ep.executor_model or creds.executor_model,
        )

    # Импортируем здесь, чтобы избежать циклических импортов на старте
    from agents.manager import AgentManager

    manager = AgentManager(creds=creds)

    async def event_stream() -> AsyncGenerator[str, None]:
        # Сначала — readiness report
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

        # Защитный «ограничитель»: гарантируем, что стрим всегда
        # корректно завершается — даже если менеджер вернёт None,
        # упадёт с исключением или клиент отвалится посреди итерации.
        try:
            gen = manager.run_task(payload.message)
            while True:
                try:
                    ev = await gen.__anext__()
                except StopAsyncIteration:
                    # Нормальное завершение генератора run_task.
                    break
                if ev is None:
                    # Менеджер иногда может вернуть None-событие; скипаем.
                    continue
                # Защита на случай, если у ProgressEvent нет to_sse()
                sse_payload = getattr(ev, "to_sse", None)
                if not callable(sse_payload):
                    log.warning("event has no to_sse() — skipping: %r", ev)
                    continue
                yield sse_payload()
        except asyncio.CancelledError:
            # Клиент закрыл SSE-соединение — корректно гасим стрим.
            log.info("client disconnected (CancelledError) mid-stream")
            try:
                await gen.aclose()
            except Exception:  # noqa: BLE001
                pass
            return
        except GeneratorExit:
            log.info("client disconnected (GeneratorExit) mid-stream")
            return
        except Exception as e:  # noqa: BLE001
            # Любая нештатная ситуация в менеджере: логируем и
            # отдаём клиенту финальный SSE-error-event, чтобы UI
            # не завис в «бесконечной загрузке».
            log.exception("unhandled error in run_task")
            try:
                yield ProgressEvent(
                    kind="error", content=f"Unhandled error: {e}"
                ).to_sse()
            except Exception:  # noqa: BLE001
                pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # отключаем буферизацию в nginx
            "Connection": "keep-alive",
        },
    )
