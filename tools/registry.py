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
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, ValidationError, create_model

from core.diagnostics import diagnostics_bus
from core.models import ToolCall, ToolResult
from tools.base import Tool
from tools.bash_tool import ExecuteBash
from tools.exceptions import SecurityError, ToolValidationError
from tools.file_tool import DeleteFile, ListDir, ReadFile, SearchInFile, WriteFile, ReplaceInFile, _safe_resolve
from tools.system_tool import GetSystemStatus
from tools.git_tool import GitTool

log = logging.getLogger("trinity.tools")



class ToolRegistry:
    """
    Реестр инструментов. По умолчанию содержит базовый Cline-подобный набор:
    execute_bash, execute_git, read_file, write_file, delete_file, search_in_file, list_dir.
    """

    def __init__(self, workspace: str = "."):
        self.workspace = workspace
        self._tools: Dict[str, Tool] = {}
        self.touched_paths: Set[str] = set()
        self.read_paths: Set[str] = set()
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Регистрирует стандартный набор инструментов."""
        for tool in (
            ExecuteBash(workspace=self.workspace),
            GitTool(workspace=self.workspace),
            ReadFile(workspace=self.workspace),
            WriteFile(workspace=self.workspace),
            ReplaceInFile(workspace=self.workspace),
            DeleteFile(workspace=self.workspace),
            SearchInFile(workspace=self.workspace),
            ListDir(workspace=self.workspace),
            GetSystemStatus(),
        ):
            self.register(tool)

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
            return
        if name in ("write_file",):
            self.touched_paths.add(path_str)
        elif name == "read_file":
            self.read_paths.add(path_str)

    def _build_validator(self, tool: Tool) -> type[BaseModel]:
        schema = tool.parameters_schema or {}
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])

        fields: Dict[str, Any] = {}
        for name, spec in properties.items():
            field_type: Any = Any
            if spec.get("type") == "string":
                field_type = str
            elif spec.get("type") == "integer":
                field_type = int
            elif spec.get("type") == "number":
                field_type = float
            elif spec.get("type") == "boolean":
                field_type = bool
            elif spec.get("type") == "array":
                field_type = list
            elif spec.get("type") == "object":
                field_type = dict
            if name in required:
                fields[name] = (field_type, ...)
            else:
                default_value = spec.get("default")
                if default_value is None and "default" not in spec:
                    fields[name] = (field_type, None)
                else:
                    fields[name] = (field_type, default_value)

        model_name = f"{tool.name.title()}Args"
        return create_model(model_name, **fields)  # type: ignore[return-value]

    def _validate_arguments(self, tool: Tool, raw_args: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if raw_args is None:
            raw_args = {}
        validator = self._build_validator(tool)
        try:
            model = validator.model_validate(raw_args)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
            )
            raise ToolValidationError(f"ToolValidationError: {tool.name}: {details}") from exc
        return model.model_dump()

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
        try:
            diagnostics_bus.publish_tool_execution(
                tool=call.name,
                args=call.arguments or {},
                call_id=call.id,
                agent=None,
            )
        except Exception as e:  # noqa: BLE001
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
            validated_args = self._validate_arguments(tool, call.arguments)
            output = await tool.execute(validated_args)
            success = True
            error = None
        except ToolValidationError as e:
            output = ""
            success = False
            error = str(e)
            log.warning("Tool validation failed for %s: %s", call.name, e)
        except SecurityError as e:
            output = ""
            success = False
            error = f"Permission denied: {e}"
            log.warning("SecurityError in %s: %s", call.name, e)
        except PermissionError as e:
            output = ""
            success = False
            error = f"Permission denied: {e}"
        except Exception as e:  # noqa: BLE001
            output = ""
            success = False
            error = f"{type(e).__name__}: {e}"
            log.exception("tool %s crashed", call.name)
        dt = int((time.perf_counter() - t0) * 1000)

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
