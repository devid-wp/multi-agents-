"""
routers/workspace.py — вынесено из main.py (Фаза 2).
Лимиты берутся из core.config.settings (workspace_max_depth / max_entries).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.config import settings

log = logging.getLogger("trinity.workspace")

try:
    from watchfiles import awatch  # type: ignore
    _WATCHFILES_AVAILABLE = True
except ImportError:  # pragma: no cover
    awatch = None  # type: ignore
    _WATCHFILES_AVAILABLE = False

router = APIRouter(prefix="/api/workspace", tags=["workspace"])

_WORKSPACE_IGNORE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode",
})
_WORKSPACE_IGNORE_FILE_SUFFIXES = (".pyc", ".pyo")

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


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
                rel = child.relative_to(workspace).as_posix()
            except ValueError:
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


@router.get("/tree")
async def workspace_tree(
    path: str = Query(".", description="Path relative to workspace"),
    hidden: int = Query(0, ge=0, le=1, description="Include hidden files (1=yes)"),
):
    if path is None:
        path = "."
    if ".." in Path(path).parts:
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    workspace = Path(settings.workspace_dir).resolve()
    entries, truncated = _walk_workspace(
        workspace,
        path,
        max_depth=settings.workspace_max_depth,
        max_entries=settings.workspace_max_entries,
        include_hidden=bool(hidden),
    )
    return JSONResponse({
        "root": str(workspace),
        "rel": path,
        "entries": entries,
        "truncated": truncated,
        "count": len(entries),
    })


@router.get("/stream")
async def workspace_stream(request: Request):
    if not _WATCHFILES_AVAILABLE or awatch is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="watchfiles is not installed. Run: pip install watchfiles")
    workspace = Path(settings.workspace_dir).resolve()

    def _should_ignore(path: Path) -> bool:
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
                        rel = p.relative_to(workspace).as_posix()
                    except Exception as e:
                        log.warning("workspace watcher gen error: %s", e)
                        continue
                    if rel.startswith("."):
                        continue
                    kind = {1: "modified", 2: "created", 3: "deleted"}.get(int(change_type), "modified")
                    yield f"data: {json.dumps({'type': kind, 'path': rel})}\n\n"
        except asyncio.CancelledError:
            return

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)
