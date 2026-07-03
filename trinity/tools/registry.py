"""
tools/registry.py
─────────────────
ToolRegistry — единая точка доступа к инструментам.

Хранит инстансы Tool, умеет:
  • выдавать схемы (для передачи в LLM)
  • исполнять ToolCall (от LLM) и возвращать ToolResult
  • отслеживать, какие файлы были тронуты (для UI)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Set

from core.models import ToolCall, ToolResult

from tools.base import Tool
from tools.bash_tool import ExecuteBash
from tools.file_tool import ListDir, ReadFile, WriteFile

log = logging.getLogger("trinity.tools")


class ToolRegistry:
    """
    Реестр инструментов. По умолчанию содержит базовый Cline-подобный набор:
    execute_bash, read_file, write_file, list_dir.
    """

    def __init__(self, workspace: str = "."):
        self.workspace = workspace
        # Имя → инстанс Tool
        self._tools: Dict[str, Tool] = {}
        # Файлы, которые были созданы/изменены (для UI)
        self.touched_paths: Set[str] = set()
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Регистрирует стандартный набор инструментов."""
        for tool in (
            ExecuteBash(workspace=self.workspace),
            ReadFile(workspace=self.workspace),
            WriteFile(workspace=self.workspace),
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

    async def execute(self, call: ToolCall, *, workspace: Optional[str] = None) -> ToolResult:
        """
        Выполняет ToolCall.
        Возвращает ToolResult со статусом success и текстовым output.
        """
        tool = self._tools.get(call.name)
        if not tool:
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                success=False,
                output="",
                error=f"Unknown tool: {call.name}. Available: {self.list_names()}",
                duration_ms=0,
            )

        t0 = time.perf_counter()
        try:
            output = await tool.execute(call.arguments)
            success = True
            error = None
        except PermissionError as e:
            output = ""
            success = False
            error = f"Permission denied: {e}"
        except Exception as e:  # noqa: BLE001
            output = ""
            success = False
            error = f"{type(e).__name__}: {e}"
        dt = int((time.perf_counter() - t0) * 1000)

        # Запоминаем «тронутые» файлы для write_file
        if call.name == "write_file" and success and "path" in call.arguments:
            self.touched_paths.add(call.arguments["path"])

        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            success=success,
            output=output,
            error=error,
            duration_ms=dt,
        )
