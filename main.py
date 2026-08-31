"""
main.py
───────
FastAPI entry-point для Trinity Multi-Agent System.

Эндпоинты:
  GET  /                  — 307 redirect → /ui/  (Mission Control Dashboard)
  GET  /ui/               — Mission Control Dashboard (новая инженерная UI)
  GET  /ui/static/*       — ассеты новой UI (Tailwind берётся с CDN)
  GET  /chat/             — legacy ChatGPT-style UI (templates/index.html)
  GET  /api/settings      — текущие настройки пользователя (ключи маскируются)
  POST /api/settings      — сохранить настройки в сессии
  POST /api/chat          — отправить задачу, получить SSE-стрим (центральная колонка)
  GET  /api/diagnostics/stream   — глобальный SSE-стрим tool_call/tool_result/error
  GET  /api/diagnostics/history  — последние N диагностических событий (newest-first)
  GET  /api/workspace/tree       — JSON-снимок дерева файлов
  GET  /api/workspace/stream     — SSE-стрим изменений файлов (watchfiles)
  GET  /api/health        — healthcheck

Запуск:
    pip install -r requirements.txt
    uvicorn main:app --reload
"""

from __future__ import annotations

import asyncio
import httpx
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.config import (
    DEFAULT_CRITIC_MODEL,
    DEFAULT_EXECUTOR_MODEL,
    DEFAULT_NVIDIA_URL,
    DEFAULT_OPENROUTER_URL,
    DEFAULT_PLANNER_MODEL,
    UserCredentials,
    settings,
)
from pydantic import BaseModel
from core.models import (
    AgentName,
    ChatRequest,
    ProgressEvent,
    SettingsPayload,
    SettingsResponse,
)
from core.session import get_credentials, mask_key, save_credentials
from core.rooms import RoomStore

# Делаем корень проекта доступным для импорта `core`, `agents`, `tools`
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import logging
from logging.handlers import RotatingFileHandler

# ───────────────────────────────────────────────────────────────────
# Логирование (Persistent + Console)
# ───────────────────────────────────────────────────────────────────
log_dir = os.path.join(BASE_DIR, "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "trinity.log")

file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
console_handler = logging.StreamHandler()

log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
formatter = logging.Formatter(log_format)
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler],
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
    if (
        len(settings.session_secret) < 32
        or settings.session_secret == "change-me-in-production-please-use-strong-secret"
    ):
        raise RuntimeError(
            "SESSION_SECRET must be a unique random value of at least 32 characters"
        )
    log.info("🚀 Trinity starting. workspace=%s", os.path.abspath(settings.workspace_dir))
    yield
    log.info("🛑 Trinity shutting down.")


# ───────────────────────────────────────────────────────────────────
# Приложение
# ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Trinity — Multi-Agent System",
    version="0.3.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def localhost_only(request: Request, call_next):
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        return JSONResponse(
            status_code=403,
            content={"detail": "Trinity local alpha accepts localhost connections only"},
        )
    # Optional local token (hardening): если TRINITY_LOCAL_TOKEN / local_token задан,
    # требуем Bearer или X-Trinity-Token на всех /api/* (UI статика без проверки)
    token = (settings.local_token or os.environ.get("TRINITY_LOCAL_TOKEN") or "").strip()
    if token and request.url.path.startswith("/api/"):
        auth = request.headers.get("authorization", "")
        xtoken = request.headers.get("x-trinity-token", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if bearer != token and xtoken != token:
            # Разрешаем health без токена, чтобы bootstrap не падал
            if request.url.path not in ("/api/health",):
                return JSONResponse(status_code=401, content={"detail": "Invalid local token"})
    return await call_next(request)

# Статика и шаблоны
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Mission Control UI: монтируем каталог ui/ как самостоятельный StaticFiles
# с html=True, чтобы /ui/ отдавал ui/index.html, а /ui/static/* — ассеты.
# Каталог может ещё не существовать при первом импорте — создаём.
UI_DIR = os.path.join(BASE_DIR, "ui")
os.makedirs(os.path.join(UI_DIR, "static"), exist_ok=True)
app.mount(
    "/ui",
    StaticFiles(directory=UI_DIR, html=True, check_dir=False),
    name="ui",
)

from routers import diagnostics as diagnostics_router
from routers import workspace as workspace_router

app.include_router(diagnostics_router.router)
app.include_router(workspace_router.router)

ACTIVE_AGENT: AgentName = AgentName.PLANNER


class AgentSwitchPayload(BaseModel):
    agent: AgentName


class RoomCreatePayload(BaseModel):
    id: str
    name: str
    strategy: str = "auto"


class ChangeDecisionPayload(BaseModel):
    approve: bool


def _room_store() -> RoomStore:
    return RoomStore(settings.workspace_dir)


@app.get("/api/rooms")
async def list_rooms():
    return {"rooms": _room_store().list()}


@app.post("/api/rooms", status_code=201)
async def create_room(payload: RoomCreatePayload):
    try:
        room = _room_store().create(payload.id, payload.name, payload.strategy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return room


@app.get("/api/changes")
async def list_changes():
    from core.changes import ChangeStore
    return {"changes": ChangeStore(settings.workspace_dir).list()}


@app.post("/api/changes/{proposal_id}/decision")
async def decide_change(proposal_id: str, payload: ChangeDecisionPayload):
    from core.changes import ChangeStore
    try:
        return ChangeStore(settings.workspace_dir).decide(proposal_id, payload.approve)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/agents/active")
async def get_active_agent():
    return {"agent": ACTIVE_AGENT}


@app.post("/api/agents/switch")
async def switch_agent(payload: AgentSwitchPayload):
    global ACTIVE_AGENT
    ACTIVE_AGENT = payload.agent
    return {"agent": ACTIVE_AGENT}


# ───────────────────────────────────────────────────────────────────
# Главная страница — redirect на Mission Control
# ───────────────────────────────────────────────────────────────────
@app.get("/", response_class=RedirectResponse, status_code=307)
async def index():
    """Корень редиректит на новую Mission Control UI."""
    return RedirectResponse(url="/ui/", status_code=307)


# ───────────────────────────────────────────────────────────────────
# Legacy chat UI (для обратной совместимости со старыми закладками)
# ───────────────────────────────────────────────────────────────────
@app.get("/chat/", response_class=HTMLResponse)
@app.get("/chat", response_class=HTMLResponse)
async def legacy_chat(request: Request):
    """Рендерит старый ChatGPT-подобный интерфейс из templates/index.html."""
    creds = get_credentials(request)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "default_openrouter_url": DEFAULT_OPENROUTER_URL,
            "default_nvidia_url": DEFAULT_NVIDIA_URL,
            "default_ollama_url": "http://localhost:11434",
            "default_planner_model": (
                (creds.planner and creds.planner.model_name) or DEFAULT_PLANNER_MODEL
            ),
            "default_critic_model": (
                (creds.critic and creds.critic.model_name) or DEFAULT_CRITIC_MODEL
            ),
            "default_executor_model": (
                (creds.executor and creds.executor.model_name) or DEFAULT_EXECUTOR_MODEL
            ),
        },
    )


# ───────────────────────────────────────────────────────────────────
# Settings API
# ───────────────────────────────────────────────────────────────────
@app.get("/api/settings", response_model=SettingsResponse)
async def read_settings(request: Request):
    creds = get_credentials(request)
    
    def _map_agent(cfg):
        if not cfg:
            return None
        return {
            "provider": cfg.provider,
            "has_key": bool(cfg.api_key and cfg.api_key.strip()),
            "key_masked": mask_key(cfg.api_key),
            "base_url": cfg.base_url,
            "model_name": cfg.model_name
        }
        
    return SettingsResponse(
        planner=_map_agent(creds.planner),
        executor=_map_agent(creds.executor),
        critic=_map_agent(creds.critic)
    )


@app.post("/api/settings")
async def write_settings(request: Request, payload: SettingsPayload):
    """Сохраняет настройки в подписанной cookie-сессии."""
    from core.models import AgentProviderConfig
    current = get_credentials(request)

    def _merge(old, new):
        if not new:
            return old
        return AgentProviderConfig(
            provider=new.provider or (old.provider if old else "nvidia"),
            api_key=new.api_key if new.api_key not in (None, "") else (old.api_key if old else None),
            base_url=_validate_url(new.base_url) or (old.base_url if old else None),
            model_name=new.model_name or (old.model_name if old else None)
        )

    new_creds = UserCredentials(
        planner=_merge(current.planner, payload.planner),
        critic=_merge(current.critic, payload.critic),
        executor=_merge(current.executor, payload.executor),
    )
    signed = save_credentials(new_creds)
    
    def _resp(cfg):
        if not cfg:
            return None
        return {
            "provider": cfg.provider,
            "has_key": bool(cfg.api_key and cfg.api_key.strip()),
            "key_masked": mask_key(cfg.api_key),
            "base_url": cfg.base_url,
            "model_name": cfg.model_name
        }

    resp = JSONResponse(
        {
            "ok": True,
            "planner": _resp(new_creds.planner),
            "critic": _resp(new_creds.critic),
            "executor": _resp(new_creds.executor),
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
    required_model = "qwen2.5-coder:1.5b"
    ollama = {
        "available": False,
        "model_installed": False,
        "required_model": required_model,
        "start_command": "ollama serve",
        "pull_command": f"ollama pull {required_model}",
    }
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            response = await client.get("http://localhost:11434/api/tags")
            response.raise_for_status()
            names = {item.get("name") for item in response.json().get("models", [])}
            ollama["available"] = True
            ollama["model_installed"] = required_model in names
    except (httpx.HTTPError, ValueError):
        pass
    return {"ok": True, "service": "trinity", "ollama": ollama}


# ───────────────────────────────────────────────────────────────────
# History API (загрузка сохранённой истории диалога)
# ───────────────────────────────────────────────────────────────────
@app.get("/api/chat/history")
async def get_history(session_id: str = Query(""), room_id: str = Query("general")):
    """
    Возвращает сохранённую историю диалога для данной сессии.
    Фронтенд вызывает этот эндпоинт при загрузке страницы, чтобы
    восстановить bridge[] без F5.
    """
    from core.history import HistoryManager
    from core.config import settings
    
    if not session_id:
        return {"ok": False, "messages": [], "error": "session_id is required"}
    
    hm = HistoryManager(workspace_dir=settings.workspace_dir)
    if not _room_store().exists(room_id):
        raise HTTPException(status_code=404, detail="room not found")
    scoped_session_id = RoomStore.session_id(session_id, room_id)
    messages = hm.load(scoped_session_id)
    return {
        "ok": True,
        "session_id": session_id,
        "room_id": room_id,
        "messages": [m.model_dump(mode="json") for m in messages]
    }


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
        from core.models import AgentProviderConfig
        
        def _merge(old, new):
            if not new:
                return old
            return AgentProviderConfig(
                provider=new.provider or (old.provider if old else "nvidia"),
                api_key=new.api_key if new.api_key not in (None, "") else (old.api_key if old else None),
                base_url=new.base_url or (old.base_url if old else None),
                model_name=new.model_name or (old.model_name if old else None)
            )

        creds = UserCredentials(
            planner=_merge(creds.planner, ep.planner),
            critic=_merge(creds.critic, ep.critic),
            executor=_merge(creds.executor, ep.executor),
        )

    # Импортируем здесь, чтобы избежать циклических импортов на старте
    from agents.manager import AgentManager

    if not _room_store().exists(payload.room_id):
        raise HTTPException(status_code=404, detail="room not found")
    scoped_session_id = RoomStore.session_id(payload.session_id or "anonymous", payload.room_id)
    manager = AgentManager(creds=creds, session_id=scoped_session_id)

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
            gen = manager.run_task(payload.message, strategy=payload.strategy or "auto")
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
