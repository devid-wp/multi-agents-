"""
tools/registry.py
─────────────────
ToolRegistry — единая точка доступа к инструментам.

Хранит инстансы Tool, умеет:
  • выдавать схемы (для передачи в LLM)
  • исполнять ToolCall (от LLM) и возвращать ToolResult
  • отслеживать, какие файлы были тронуты (для UI)
  • эмитить диагностические события в core.diagnostics.diagnostics_bus
    на КАЖДЫЙ вызов (включая заблокированные sandbox-ом), чтобы
    Live Log Stream видел полную картину.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core.diagnostics import diagnostics_bus
from core.models import ToolCall, ToolResult

from tools.base import Tool
from tools.bash_tool import ExecuteBash
from tools.exceptions import SecurityError
from tools.file_tool import DeleteFile, ListDir, ReadFile, SearchInFile, WriteFile, _safe_resolve

log = logging.getLogger("trinity.tools")


class ToolRegistry:
    """
    Реестр инструментов. По умолчанию содержит базовый Cline-подобный набор:
    execute_bash, read_file, write_file, delete_file, search_in_file, list_dir.
    """

    def __init__(self, workspace: str = "."):
        self.workspace = workspace
        # Имя → инстанс Tool
        self._tools: Dict[str, Tool] = {}
        # Файлы, которые были созданы/изменены (для UI)
        self.touched_paths: Set[str] = set()
        # Файлы, которые были прочитаны (для UI/контекста)
        self.read_paths: Set[str] = set()
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Регистрирует стандартный набор инструментов."""
        for tool in (
            ExecuteBash(workspace=self.workspace),
            ReadFile(workspace=self.workspace),
            WriteFile(workspace=self.workspace),
            DeleteFile(workspace=self.workspace),
            SearchInFile(workspace=self.workspace),
            ListDir(workspace=self.workspace),
        ):
            self.register(tool)

    # ── Публичный API ─────────────────────────────────────────────
    def register(self, tool: Tool) -> None:
        """Регистрирует новый инструмент (или переопределяет существующий)."""
        if not tool.name:
            raise ValueError("Tool.name must be set")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_names(self) -> List[str]:
        return list(self._tools.keys())

    def list_schemas(self) -> List[Dict[str, Any]]:
        """OpenAI-схемы всех инструментов, для передачи в LLM."""
        return [t.to_openai_schema() for t in self._tools.values()]

    def _track_path(self, name: str, arguments: Dict[str, Any]) -> None:
        """
        Запоминает путь, к которому обращался агент.
        Резолвим относительно workspace, чтобы в UI не было сырых
        '../' и дубликатов типа './foo' vs 'foo'.
        """
        raw = arguments.get("path")
        if not raw:
            return
        try:
            resolved = _safe_resolve(self.workspace, raw)
            path_str = str(resolved)
        except PermissionError:
            # Не выезжает за песочницу — в UI не показываем, но и не падаем.
            return
        if name in ("write_file",):
            self.touched_paths.add(path_str)
        elif name == "read_file":
            self.read_paths.add(path_str)

    async def execute(self, call: ToolCall, *, workspace: Optional[str] = None) -> ToolResult:
        """
        Выполняет ToolCall.
        Возвращает ToolResult со статусом success и текстовым output.

        Поток событий:
          1) ДО любых проверок публикуем tool_execution в diagnostics_bus —
             это попадёт в Live Log Stream и покажет пользователю, КУДА
             LLM полезла (включая попытки выйти за sandbox).
          2) Если tool не найден — публикуем ошибку, возвращаем failed ToolResult.
          3) Иначе запускаем tool.execute(arguments).
          4) SecurityError (выход за workspace) → ToolResult с success=False
             и error, начинающимся с "Permission denied: " (UI фильтрует
             по этому префиксу).
        """
        # 1) Эмитим «вход» в tool. Делаем ЭТО ДО проверки существования
        #    tool-а: даже неизвестный tool должен быть виден в логе.
        try:
            diagnostics_bus.publish_tool_execution(
                tool=call.name,
                args=call.arguments or {},
                call_id=call.id,
                agent=None,  # выставляется на уровне AgentContext, если есть
            )
        except Exception as e:  # noqa: BLE001
            # Сломанная шина не должна валить выполнение tool-а.
            log.warning("diagnostics_bus.publish_tool_execution failed: %s", e)

        tool = self._tools.get(call.name)
        if not tool:
            err = f"Unknown tool: {call.name}. Available: {self.list_names()}"
            log.warning("ToolRegistry: %s", err)
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                success=False,
                output="",
                error=err,
                duration_ms=0,
            )

        t0 = time.perf_counter()
        try:
            output = await tool.execute(call.arguments)
            success = True
            error = None
        except SecurityError as e:
            # Sandbox сработал. Поднимаем в лог отдельной строкой — это
            # инцидент безопасности, должен быть виден.
            output = ""
            success = False
            error = f"Permission denied: {e}"
            log.warning("SecurityError in %s: %s", call.name, e)
        except PermissionError as e:
            # Любой другой PermissionError (например, OS-уровня).
            output = ""
            success = False
            error = f"Permission denied: {e}"
        except Exception as e:  # noqa: BLE001
            output = ""
            success = False
            error = f"{type(e).__name__}: {e}"
            log.exception("tool %s crashed", call.name)
        dt = int((time.perf_counter() - t0) * 1000)

        # Запоминаем тронутые/прочитанные пути (только при успехе).
        if success and call.name in ("read_file", "write_file"):
            self._track_path(call.name, call.arguments)

        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            success=success,
            output=output,
            error=error,
            duration_ms=dt,
        )
