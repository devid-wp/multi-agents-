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
from typing import Any, Dict, List

from tools.base import Tool


def _safe_resolve(workspace: str, user_path: str) -> str:
    """
    Проверяет, что итоговый путь остаётся внутри workspace.
    Возвращает абсолютный путь или кидает PermissionError.
    """
    workspace_abs = os.path.abspath(workspace)
    if not user_path:
        return workspace_abs
    # Если путь относительный — склеиваем с workspace
    candidate = (
        user_path
        if os.path.isabs(user_path)
        else os.path.join(workspace_abs, user_path)
    )
    resolved = os.path.abspath(candidate)
    # Проверка «внутри workspace»
    if not (resolved == workspace_abs or resolved.startswith(workspace_abs + os.sep)):
        raise PermissionError(
            f"Path '{user_path}' resolves outside workspace '{workspace_abs}'."
        )
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

    def __init__(self, workspace: str = "."):
        self.workspace = workspace

    async def execute(self, arguments: Dict[str, Any]) -> str:
        path = _safe_resolve(self.workspace, arguments["path"])
        if not os.path.isfile(path):
            return f"[ERROR] Not a file: {path}"
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:  # noqa: BLE001
            return f"[ERROR] Cannot read {path}: {e}"
        if len(content) > 50_000:
            content = content[:50_000] + "\n\n[...truncated; file is larger than 50k chars...]"
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
        path = _safe_resolve(self.workspace, arguments["path"])
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(arguments["content"])
        except PermissionError as e:
            return f"[BLOCKED] {e}"
        except Exception as e:  # noqa: BLE001
            return f"[ERROR] Write failed: {e}"
        return f"[OK] Wrote {len(arguments['content'])} bytes to {path}"


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

    def __init__(self, workspace: str = "."):
        self.workspace = workspace

    async def execute(self, arguments: Dict[str, Any]) -> str:
        user_path = arguments.get("path", ".") or "."
        path = _safe_resolve(self.workspace, user_path)
        max_entries = int(arguments.get("max_entries", 200))
        if not os.path.isdir(path):
            return f"[ERROR] Not a directory: {path}"

        try:
            entries: List[str] = []
            for name in sorted(os.listdir(path)):
                if name.startswith("."):
                    continue
                full = os.path.join(path, name)
                if os.path.isdir(full):
                    entries.append(f"📁 {name}/")
                else:
                    size = os.path.getsize(full)
                    entries.append(f"📄 {name}  ({size} B)")
                if len(entries) >= max_entries:
                    entries.append(f"... truncated at {max_entries} entries ...")
                    break
        except Exception as e:  # noqa: BLE001
            return f"[ERROR] list_dir failed: {e}"
        return "\n".join(entries) if entries else "(empty directory)"
