"""
main.py — FastAPI entry-point (0.7.1).

Эндпоинты вынесены в routers/*:
  routers/workspace  — /api/workspace/tree|file|stream
  routers/diagnostics— /api/diagnostics/*
  routers/rooms      — /api/rooms
  routers/changes    — /api/changes
  routers/agents     — /api/agents/*
  routers/chat       — POST /api/chat + GET /api/chat/history
  routers/system     — /api/settings, /api/health
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from core.config import settings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from logging.handlers import RotatingFileHandler

log_dir = os.path.join(BASE_DIR, "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "trinity.log")
file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
console_handler = logging.StreamHandler()
log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
formatter = logging.Formatter(log_format)
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)
logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
log = logging.getLogger("trinity.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if len(settings.session_secret) < 32 or settings.session_secret == "change-me-in-production-please-use-strong-secret":
        raise RuntimeError("SESSION_SECRET must be a unique random value of at least 32 characters")
    log.info("🚀 Trinity starting. workspace=%s", os.path.abspath(settings.workspace_dir))
    try:
        from core.db import init_db, is_enabled, migrate_json_if_needed

        if is_enabled():
            init_db(settings.workspace_dir)
            migrate_json_if_needed(settings.workspace_dir)
            log.info("SQLite backend enabled at %s", os.path.join(settings.workspace_dir, ".trinity/trinity.db"))
    except Exception as e:  # noqa: BLE001
        log.warning("SQLite init/migrate failed: %s", e)
    yield
    log.info("🛑 Trinity shutting down.")


app = FastAPI(title="Trinity — Multi-Agent System", version="0.7.1", lifespan=lifespan)


@app.middleware("http")
async def localhost_only(request: Request, call_next):
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        return JSONResponse(status_code=403, content={"detail": "Trinity local alpha accepts localhost connections only"})
    token = (settings.local_token or os.environ.get("TRINITY_LOCAL_TOKEN") or "").strip()
    if token and request.url.path.startswith("/api/"):
        auth = request.headers.get("authorization", "")
        xtoken = request.headers.get("x-trinity-token", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if bearer != token and xtoken != token:
            if request.url.path not in ("/api/health",):
                return JSONResponse(status_code=401, content={"detail": "Invalid local token"})
    return await call_next(request)

# Mission Control UI: Vite build -> dist/ primary, fallback ui/
DIST_DIR = os.path.join(BASE_DIR, "dist")
UI_DIR = os.path.join(BASE_DIR, "ui")
os.makedirs(os.path.join(UI_DIR, "static"), exist_ok=True)
_UI_DIR_TO_SERVE = DIST_DIR if (os.path.isdir(DIST_DIR) and os.path.exists(os.path.join(DIST_DIR, "index.html"))) else UI_DIR
if _UI_DIR_TO_SERVE == DIST_DIR:
    log.info("Serving Vite build from dist/ at /ui/")
else:
    log.info("Serving dev UI from ui/ at /ui/ (run `npm run build` for dist)")
app.mount("/ui", StaticFiles(directory=_UI_DIR_TO_SERVE, html=True, check_dir=False), name="ui")

from routers import agents as agents_router
from routers import changes as changes_router
from routers import chat as chat_router
from routers import diagnostics as diagnostics_router
from routers import rooms as rooms_router
from routers import system as system_router
from routers import workspace as workspace_router

app.include_router(workspace_router.router)
app.include_router(diagnostics_router.router)
app.include_router(rooms_router.router)
app.include_router(changes_router.router)
app.include_router(agents_router.router)
app.include_router(chat_router.router)
app.include_router(system_router.router)


@app.get("/", response_class=RedirectResponse, status_code=307)
async def index():
    return RedirectResponse(url="/ui/", status_code=307)
