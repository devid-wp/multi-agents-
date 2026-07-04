"""
tools/file_tool.py
──────────────────
Четыре файловых инструмента (Cline-подобные):

  • read_file     — прочитать содержимое
  • write_file    — создать/перезаписать
  • delete_file   — удалить файл (защищён от сноса директорий)
  • search_in_file — grep с sandbox-проверкой (файл или директория)
  • list_dir      — посмотреть директорию

Все они работают ТОЛЬКО в пределах workspace_dir.
Любой выход за его пределы → SecurityError (наследник PermissionError,
импортируется из tools/exceptions.py и re-export-ится здесь для удобства).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Union

from tools.base import Tool
from tools.exceptions import SecurityError

PathLike = Union[str, os.PathLike]

__all__ = [
    "SecurityError",
    "_safe_resolve",
    "ReadFile",
    "WriteFile",
    "DeleteFile",
    "SearchInFile",
    "ListDir",
]


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
        # Битый путь (например, слишком длинный) — это не Security, это
        # нормальная ошибка, которую LLM должна увидеть. Поднимаем как
        # SecurityError всё равно, чтобы реестр ловил единообразно.
        raise SecurityError(f"Cannot resolve path {user_path_str!r}: {e}") from e

    workspace_root = Path(workspace).resolve()
    # Корректная проверка «внутри»: добавляем os.sep, чтобы /workspace-evil
    # не считался поддиректорией /workspace.
    try:
        resolved.relative_to(workspace_root)
    except ValueError as e:
        raise SecurityError(
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


# ───────────────────────────────────────────────────────────────────
# Delete
# ───────────────────────────────────────────────────────────────────
class DeleteFile(Tool):
    """
    Удаляет один файл. Удалять директории запрещено на уровне этого tool-а —
    иначе LLM одной командой снесёт пол-проекта. Для работы с директориями
    есть list_dir (read-only) и будущий tree-remove.
    """

    name = "delete_file"
    description = (
        "Delete a single file inside the workspace. Refuses to delete "
        "directories (use a dedicated tool for that). Path is sandboxed."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to delete (relative to workspace or absolute).",
            },
        },
        "required": ["path"],
    }

    def __init__(self, workspace: str = "."):
        self.workspace = workspace

    async def execute(self, arguments: Dict[str, Any]) -> str:
        path_arg = arguments.get("path")
        if not path_arg:
            return "[ERROR] 'path' argument is required for delete_file."

        # 1) Sandbox-резолв. SecurityError → [BLOCKED] в LLM-виде.
        try:
            path = _safe_resolve(self.workspace, path_arg)
        except SecurityError as e:
            return f"[BLOCKED] {e}"

        # 2) Проверка существования.
        if not path.exists():
            return f"[ERROR] File not found: {path}"

        # 3) Не даём LLM случайно снести директорию через delete_file.
        if path.is_dir() or path.is_symlink() and path.is_dir():
            return (
                f"[ERROR] {path} is a directory; delete_file only removes files. "
                "Use a directory-management tool if you really need to remove a tree."
            )

        # 4) Удаление.
        try:
            path.unlink()
        except PermissionError as e:
            return f"[BLOCKED] OS-level delete denied for {path}: {e}"
        except OSError as e:
            return f"[ERROR] Delete failed for {path}: {e}"

        return f"[OK] Deleted {path}"


# ───────────────────────────────────────────────────────────────────
# Search (grep с sandbox)
# ───────────────────────────────────────────────────────────────────
class SearchInFile(Tool):
    """
    Ищет regex в одном файле или рекурсивно в директории.

    Ограничения:
      • По умолчанию — 100 матчей, hard cap — 1000 (чтобы LLM не утонула).
      • На рекурсивном обходе — КАЖДЫЙ найденный файл снова прогоняется
        через _safe_resolve (защита от symlink-ловушек, ведущих наружу).
      • Бинарные файлы пропускаются (heuristic: NUL-байт в первых 8 KiB).
    """

    name = "search_in_file"
    description = (
        "Search a regex pattern in a file or recursively in a directory. "
        "Returns matches as 'line_no: line'. Truncated to max_results."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File or directory to search in (relative to workspace or absolute).",
            },
            "pattern": {
                "type": "string",
                "description": "Regular expression (Python re syntax).",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of matches to return (default 100, hard cap 1000).",
                "default": 100,
                "minimum": 1,
                "maximum": 1000,
            },
        },
        "required": ["path", "pattern"],
    }

    DEFAULT_MAX = 100
    HARD_MAX = 1000
    BINARY_SNIFF_BYTES = 8 * 1024

    def __init__(self, workspace: str = "."):
        self.workspace = workspace

    # ── helpers ─────────────────────────────────────────────────
    @staticmethod
    def _is_probably_binary(path: Path) -> bool:
        """Эвристика: NUL в первых 8 KiB ⇒ бинарь, не лезем в него."""
        try:
            with path.open("rb") as fh:
                chunk = fh.read(SearchInFile.BINARY_SNIFF_BYTES)
        except OSError:
            return True  # не прочиталось — на всякий случай пропустим
        return b"\x00" in chunk

    def _search_file(self, file_path: Path, regex: re.Pattern, results: List[str], max_results: int) -> bool:
        """
        Ищет regex в одном файле. Возвращает True, если достигнут лимит
        (тогда вызывающий должен прервать обход).
        """
        if self._is_probably_binary(file_path):
            return False
        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line_no, line in enumerate(fh, start=1):
                    if regex.search(line):
                        # Чистим line от трейлинг-нулей, чтобы не рвать вывод
                        results.append(f"{file_path}:{line_no}: {line.rstrip()}")
                        if len(results) >= max_results:
                            return True
        except OSError:
            return False
        return False

    def _search_directory(
        self,
        dir_path: Path,
        regex: re.Pattern,
        results: List[str],
        max_results: int,
    ) -> None:
        """
        Рекурсивный обход с sandbox-проверкой на каждом файле.
        """
        # sorted() даёт детерминированный порядок (важно для тестов)
        for root, dirs, files in os.walk(dir_path):
            # Не уходим в скрытые каталоги (.git, .venv, __pycache__)
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files):
                if name.startswith("."):
                    continue
                file_path = Path(root) / name
                # Доп. sandbox-проверка: файл мог оказаться ПОЗАДИ
                # symlink-а, уводящего из workspace.
                try:
                    safe = _safe_resolve(self.workspace, file_path)
                except SecurityError:
                    continue
                if self._search_file(safe, regex, results, max_results):
                    return  # лимит

    # ── public ──────────────────────────────────────────────────
    async def execute(self, arguments: Dict[str, Any]) -> str:
        path_arg = arguments.get("path")
        pattern = arguments.get("pattern")
        if not path_arg:
            return "[ERROR] 'path' argument is required for search_in_file."
        if not pattern or not isinstance(pattern, str):
            return "[ERROR] 'pattern' argument is required and must be a string."

        # max_results
        try:
            max_results = int(arguments.get("max_results", self.DEFAULT_MAX))
        except (TypeError, ValueError):
            return "[ERROR] 'max_results' must be an integer."
        if max_results < 1:
            return "[ERROR] 'max_results' must be >= 1."
        max_results = min(max_results, self.HARD_MAX)

        # regex
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"[ERROR] Invalid regex {pattern!r}: {e}"

        # sandbox
        try:
            path = _safe_resolve(self.workspace, path_arg)
        except SecurityError as e:
            return f"[BLOCKED] {e}"

        if not path.exists():
            return f"[ERROR] Path not found: {path}"

        # go
        results: List[str] = []
        try:
            if path.is_file():
                self._search_file(path, regex, results, max_results)
            elif path.is_dir():
                self._search_directory(path, regex, results, max_results)
            else:
                return f"[ERROR] Not a regular file or directory: {path}"
        except Exception as e:  # noqa: BLE001
            return f"[ERROR] Search failed for {path}: {e}"

        if not results:
            return f"(no matches for {pattern!r} under {path})"
        if len(results) >= max_results:
            results.append(f"... truncated at {max_results} matches ...")
        return "\n".join(results)
