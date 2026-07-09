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

from core.config import DEFAULT_NVIDIA_URL, UserCredentials, settings
from core.diagnostics import diagnostics_bus
from pydantic import BaseModel
from core.models import (
    AgentName,
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

ACTIVE_AGENT: AgentName = AgentName.PLANNER


class AgentSwitchPayload(BaseModel):
    agent: AgentName


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
    return {"ok": True, "service": "trinity"}


# ───────────────────────────────────────────────────────────────────
# History API (загрузка сохранённой истории диалога)
# ───────────────────────────────────────────────────────────────────
@app.get("/api/chat/history")
async def get_history(session_id: str = Query("")):
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
    messages = hm.load(session_id)
    return {
        "ok": True,
        "session_id": session_id,
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

    manager = AgentManager(creds=creds, session_id=payload.session_id)

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


# ───────────────────────────────────────────────────────────────────
# Live Diagnostics — глобальный SSE + история
#
#   /api/diagnostics/stream   — long-lived SSE, фанит-аутит ВСЕ
#                               tool_call/tool_result/error от ВСЕХ
#                               параллельных прогонов (через шину).
#   /api/diagnostics/history  — последние N событий из ring buffer.
# ───────────────────────────────────────────────────────────────────
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@app.get("/api/diagnostics/stream")
async def diagnostics_stream(request: Request):
    """
    Persistent SSE: фанит-аутит tool_call/tool_result/error от шины.

    Протокол:
      : ready\\n\\n              — посылаем сразу после подписки
      data: <ProgressEvent JSON>\\n\\n   — каждое событие
      : ping\\n\\n               — раз в ~15s, чтобы прокси не закрыли idle
    """
    queue = diagnostics_bus.subscribe()

    async def gen() -> AsyncGenerator[str, None]:
        try:
            yield ": ready\n\n"
            ping_at = asyncio.get_event_loop().time() + 15.0
            while True:
                # Ждём либо нового события, либо таймаута для ping
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

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@app.get("/api/diagnostics/history")
async def diagnostics_history(limit: int = Query(200, ge=1, le=500)):
    """Снимок последних диагностических событий (newest-first)."""
    return JSONResponse(diagnostics_bus.history(limit=limit))


# ───────────────────────────────────────────────────────────────────
# Workspace tree (REST snapshot + SSE file-watcher)
# ───────────────────────────────────────────────────────────────────
# watchfiles — внешний dep, импортируем лениво, чтобы /api/health
# не падал, если пакет ещё не установлен в окружении.
try:
    from watchfiles import awatch  # type: ignore
    _WATCHFILES_AVAILABLE = True
except ImportError:  # pragma: no cover
    awatch = None  # type: ignore
    _WATCHFILES_AVAILABLE = False


# Каталоги, которые НЕ показываем в дереве и не трекаем в watcher-е.
_WORKSPACE_IGNORE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode",
})
_WORKSPACE_IGNORE_FILE_SUFFIXES = (".pyc", ".pyo")


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def _walk_workspace(
    workspace: Path,
    rel_root: str,
    *,
    max_depth: int = 4,
    max_entries: int = 1000,
    include_hidden: bool = False,
) -> tuple[list[dict], bool]:
    """
    Рекурсивный обход workspace. Возвращает (entries, truncated).
    entries: плоский список {"path","type","size","mtime"} (path — относительно workspace).
    """
    base = (workspace / rel_root).resolve() if rel_root not in ("", ".") else workspace.resolve()
    entries: list[dict] = []
    truncated = False

    def _walk(cur: Path, depth: int) -> None:
        nonlocal truncated
        if len(entries) >= max_entries:
            truncated = True
            return
        if depth > max_depth:
            return
        try:
            children = sorted(cur.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            return
        for child in children:
            if len(entries) >= max_entries:
                truncated = True
                return
            name = child.name
            if not include_hidden and _is_hidden(name):
                continue
            if child.is_dir() and name in _WORKSPACE_IGNORE_DIRS:
                continue
            if child.is_file() and name.endswith(_WORKSPACE_IGNORE_FILE_SUFFIXES):
                continue
            try:
                st = child.stat()
                size = st.st_size if child.is_file() else 0
                mtime = st.st_mtime
            except OSError:
                continue
            try:
                rel = str(child.relative_to(workspace))
            except ValueError:
                # Не внутри workspace (симлинк наружу) — пропускаем
                continue
            entries.append({
                "path": rel,
                "type": "dir" if child.is_dir() else "file",
                "size": int(size),
                "mtime": float(mtime),
            })
            if child.is_dir():
                _walk(child, depth + 1)

    _walk(base, 0)
    return entries, truncated


@app.get("/api/workspace/tree")
async def workspace_tree(
    path: str = Query(".", description="Path relative to workspace"),
    hidden: int = Query(0, ge=0, le=1, description="Include hidden files (1=yes)"),
):
    """
    JSON-снимок дерева файлов.

    path — относительный путь внутри settings.workspace_dir. '..' запрещён.
    Ограничения: depth ≤ 4, entries ≤ 1000 (см. флаг truncated).
    """
    # Защита от path traversal
    if path is None:
        path = "."
    if ".." in Path(path).parts:
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    workspace = Path(settings.workspace_dir).resolve()
    entries, truncated = _walk_workspace(
        workspace,
        path,
        max_depth=4,
        max_entries=1000,
        include_hidden=bool(hidden),
    )
    return JSONResponse({
        "root": str(workspace),
        "rel": path,
        "entries": entries,
        "truncated": truncated,
        "count": len(entries),
    })


@app.get("/api/workspace/stream")
async def workspace_stream(request: Request):
    """
    SSE-поток изменений в workspace. Бэкенд — watchfiles.awatch.

    Каждое изменение (created / modified / deleted) присылается
    относительным путём. Игнорируемые каталоги (см. _WORKSPACE_IGNORE_DIRS)
    и *.pyc фильтруются здесь, чтобы UI не дёргался на каждое движение
    внутри __pycache__.
    """
    if not _WATCHFILES_AVAILABLE or awatch is None:  # pragma: no cover
        raise HTTPException(
            status_code=503,
            detail="watchfiles is not installed. Run: pip install watchfiles",
        )

    workspace = Path(settings.workspace_dir).resolve()

    def _should_ignore(path: Path) -> bool:
        # Проверяем по сегментам пути — если любой из родителей
        # входит в игнор-список, событие пропускаем.
        for part in path.parts:
            if part in _WORKSPACE_IGNORE_DIRS:
                return True
            if part.endswith(_WORKSPACE_IGNORE_FILE_SUFFIXES):
                return True
        return False

    async def gen() -> AsyncGenerator[str, None]:
        yield ": ready\n\n"
        try:
            async for changes in awatch(workspace, step=200, recursive=True):
                for change_type, abs_path in changes:
                    p = Path(abs_path)
                    if _should_ignore(p):
                        continue
                    try:
                        rel = str(p.relative_to(workspace))
                    except Exception as e:
                        log.warning("workspace watcher gen error: %s", e)
                        continue
                    if rel.startswith("."):
                        continue
                    # change_type: 1=modified, 2=created, 3=deleted
                    # см. watchfiles.Change
                    kind = {1: "modified", 2: "created", 3: "deleted"}.get(
                        int(change_type), "modified"
                    )
                    yield f"data: {json.dumps({'type': kind, 'path': rel})}\n\n"
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
