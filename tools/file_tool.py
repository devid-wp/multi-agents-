"""
tools/file_tool.py
──────────────────
Три файловых инструмента (Cline-подобные):

  • read_file     — прочитать содержимое
  • write_file    — создать/перезаписать
  • list_dir      — посмотреть директорию

Все они работают ТОЛЬКО в пределах workspace_dir
(любой выход за его пределы → PermissionError).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Union

from tools.base import Tool

PathLike = Union[str, os.PathLike]


# ───────────────────────────────────────────────────────────────────
# Sandbox
# ───────────────────────────────────────────────────────────────────
def _safe_resolve(workspace: PathLike, user_path: PathLike) -> Path:
    """
    Резолвит пользовательский путь в абсолютный и проверяет, что итог
    остаётся внутри workspace.

    Используем Path.resolve() (а не просто os.path.abspath), потому что:
      • resolve() разворачивает симлинки — иначе LLM может передать
        /workspace/safe/../../etc/passwd и пройти проверку;
      • resolve() нормализует регистр (важно на macOS, и не мешает на Linux);
      • strict=False — путь может не существовать (нужно для write_file).

    Возвращает Path или кидает PermissionError.
    """
    if user_path is None or str(user_path).strip() == "":
        return Path(workspace).resolve()

    user_path_str = os.fspath(user_path)
    candidate = Path(user_path_str)

    # Относительный путь склеиваем с workspace; абсолютный оставляем как есть.
    if not candidate.is_absolute():
        candidate = Path(workspace) / candidate

    try:
        resolved = candidate.resolve(strict=False)
    except OSError as e:
        # Битый путь (например, слишком длинный) — это не Permission, это
        # нормальная ошибка, которую LLM должна увидеть.
        raise PermissionError(f"Cannot resolve path {user_path_str!r}: {e}") from e

    workspace_root = Path(workspace).resolve()
    # Корректная проверка «внутри»: добавляем os.sep, чтобы /workspace-evil
    # не считался поддиректорией /workspace.
    try:
        resolved.relative_to(workspace_root)
    except ValueError as e:
        raise PermissionError(
            f"Path {user_path_str!r} resolves outside workspace "
            f"'{workspace_root}'. Refusing to operate."
        ) from e

    return resolved


# ───────────────────────────────────────────────────────────────────
# Read
# ───────────────────────────────────────────────────────────────────
class ReadFile(Tool):
    name = "read_file"
    description = (
        "Read the contents of a file. Returns the file content as a UTF-8 string, "
        "truncated to 50 000 chars. Use to inspect existing code or data."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file (relative to workspace or absolute).",
            },
        },
        "required": ["path"],
    }

    # Жёсткий потолок, чтобы один гигантский лог не утопил контекст LLM.
    MAX_CHARS = 50_000

    def __init__(self, workspace: str = "."):
        self.workspace = workspace

    async def execute(self, arguments: Dict[str, Any]) -> str:
        path_arg = arguments.get("path")
        if not path_arg:
            return "[ERROR] 'path' argument is required for read_file."

        try:
            path = _safe_resolve(self.workspace, path_arg)
        except PermissionError as e:
            return f"[BLOCKED] {e}"

        if not path.exists():
            return f"[ERROR] File not found: {path}"
        if not path.is_file():
            return f"[ERROR] Not a regular file: {path}"

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except PermissionError as e:
            return f"[BLOCKED] OS-level read denied for {path}: {e}"
        except OSError as e:
            return f"[ERROR] Cannot read {path}: {e}"

        if len(content) > self.MAX_CHARS:
            content = (
                content[: self.MAX_CHARS]
                + f"\n\n[...truncated; file is larger than {self.MAX_CHARS} chars...]"
            )
        return content


# ───────────────────────────────────────────────────────────────────
# Write
# ───────────────────────────────────────────────────────────────────
class WriteFile(Tool):
    name = "write_file"
    description = (
        "Create a new file or completely overwrite an existing one with the given "
        "UTF-8 content. Parent directories are created automatically."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Destination path (relative to workspace or absolute).",
            },
            "content": {
                "type": "string",
                "description": "Full file content.",
            },
        },
        "required": ["path", "content"],
    }

    def __init__(self, workspace: str = "."):
        self.workspace = workspace

    async def execute(self, arguments: Dict[str, Any]) -> str:
        path_arg = arguments.get("path")
        if not path_arg:
            return "[ERROR] 'path' argument is required for write_file."

        content = arguments.get("content")
        if not isinstance(content, str):
            return "[ERROR] 'content' must be a string."

        try:
            path = _safe_resolve(self.workspace, path_arg)
        except PermissionError as e:
            return f"[BLOCKED] {e}"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except PermissionError as e:
            return f"[BLOCKED] OS-level write denied for {path}: {e}"
        except OSError as e:
            return f"[ERROR] Write failed for {path}: {e}"

        return f"[OK] Wrote {len(content)} bytes to {path}"


# ───────────────────────────────────────────────────────────────────
# List
# ───────────────────────────────────────────────────────────────────
class ListDir(Tool):
    name = "list_dir"
    description = (
        "List the contents of a directory (files and sub-directories), "
        "with their sizes. Hidden files (starting with '.') are skipped."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory to list. Default: workspace root.",
                "default": ".",
            },
            "max_entries": {
                "type": "integer",
                "description": "Maximum number of entries to return (default 200).",
                "default": 200,
                "minimum": 1,
                "maximum": 5000,
            },
        },
        "required": [],
    }

    DEFAULT_MAX = 200
    HARD_MAX = 5_000

    def __init__(self, workspace: str = "."):
        self.workspace = workspace

    async def execute(self, arguments: Dict[str, Any]) -> str:
        user_path = arguments.get("path", ".") or "."

        try:
            path = _safe_resolve(self.workspace, user_path)
        except PermissionError as e:
            return f"[BLOCKED] {e}"

        if not path.exists():
            return f"[ERROR] Directory not found: {path}"
        if not path.is_dir():
            return f"[ERROR] Not a directory: {path}"

        # Жёсткий потолок, чтобы LLM не заказала себе миллион записей.
        try:
            max_entries = int(arguments.get("max_entries", self.DEFAULT_MAX))
        except (TypeError, ValueError):
            return "[ERROR] 'max_entries' must be an integer."
        if max_entries < 1:
            return "[ERROR] 'max_entries' must be >= 1."
        max_entries = min(max_entries, self.HARD_MAX)

        try:
            entries: List[str] = []
            for name in sorted(path.iterdir()):
                if name.name.startswith("."):
                    continue
                if name.is_dir():
                    entries.append(f"📁 {name.name}/")
                elif name.is_file():
                    try:
                        size = name.stat().st_size
                    except OSError:
                        size = -1  # сломанный симлинк, размер неизвестен
                    entries.append(f"📄 {name.name}  ({size} B)")
                else:
                    # Сокеты, fifo, блочные устройства — пометить явно.
                    entries.append(f"🔗 {name.name}  (special)")

                if len(entries) >= max_entries:
                    entries.append(f"... truncated at {max_entries} entries ...")
                    break
        except PermissionError as e:
            return f"[BLOCKED] OS-level list denied for {path}: {e}"
        except OSError as e:
            return f"[ERROR] list_dir failed for {path}: {e}"

        return "\n".join(entries) if entries else "(empty directory)"
