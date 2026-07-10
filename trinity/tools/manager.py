"""
trinity/tools/manager.py
────────────────────────
ClineToolManager — загрузчик JSON-схем и диспетчер вызовов для 6 инструментов
Cline, портированных в `trinity.tools.executors`.

Зачем нужен:
  • `schemas.json` (mirror of `extracted_tools/schemas.json`) лежит рядом.
  • Gemini ожидает schemas в формате `functionDeclarations` внутри
    `payload["tools"]`. Менеджер превращает «сырые» JSON-схемы в этот
    формат, а также в OpenAI-стиль (для совместимости с другими
    провайдерами, если потребуется).
  • Когда Gemini возвращает `functionCall`, менеджер маршрутизирует
    вызов в нужный Python-executor и формирует структурированный ответ
    (тот же `ToolOperationResult`, что выдают исполнители).

Использование:
    mgr = ClineToolManager(workspace=settings.workspace_dir)
    schemas_gemini = mgr.gemini_function_declarations()  # → list[dict]
    # ... передаём schemas_gemini в GoogleGeminiClient.chat(tools=...)
    results = await mgr.run(tool_name, arguments)        # → list[ToolOperationResult]
    text = mgr.format_tool_response(tool_name, results)  # → str (для tool_response)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from trinity.tools import executors
from trinity.tools.executors import ToolOperationResult, dispatch

log = logging.getLogger("trinity.tool_manager")


_SCHEMAS_FILENAME = "schemas.json"


def _default_schemas_path() -> str:
    """Файл схем, лежащий в пакете `trinity.tools`."""
    return str(Path(__file__).resolve().parent / _SCHEMAS_FILENAME)


# Если в проекте также лежит `extracted_tools/schemas.json` — отдаём
# приоритет ему (там «эталонные» схемы из Cline), а наш `trinity.tools.schemas`
# служит fallback-копией.
_EXTRACTED_SCHEMAS_CANDIDATES = (
    # walked from CWD up — covers the project root regardless of where
    # uvicorn was launched.
    "extracted_tools/schemas.json",
)


def _find_extracted_schemas() -> Optional[str]:
    here = Path(__file__).resolve()
    for ancestor in (here.parent, *here.parents):
        for rel in _EXTRACTED_SCHEMAS_CANDIDATES:
            cand = ancestor / rel
            if cand.is_file():
                return str(cand)
    return None


@dataclass
class ClineToolManager:
    """
    Менеджер шести инструментов Cline (Python-порт).

    Параметры:
      workspace       — корневая директория sandbox. Все файловые операции
                        ограничены этой папкой (через `os.path.abspath`).
      schemas_path    — путь к schemas.json (по умолчанию ищем
                        `extracted_tools/schemas.json` от корня проекта;
                        fallback — `trinity/tools/schemas.json`).
      auto_approve    — если True, `run_commands` НЕ требует Y/N подтверждения.
                        В production должно быть False. Используется в тестах.
    """

    workspace: str = "."
    schemas_path: Optional[str] = None
    auto_approve: bool = False

    _schemas: Optional[Dict[str, Dict[str, Any]]] = None
    _lock: Optional[asyncio.Lock] = None

    def __post_init__(self) -> None:
        self.workspace = os.path.abspath(self.workspace or ".")
        if not self.schemas_path:
            self.schemas_path = (
                _find_extracted_schemas() or _default_schemas_path()
            )

    # ------------------------------------------------------------------
    # Schema loading
    # ------------------------------------------------------------------
    def load_schemas(self) -> Dict[str, Dict[str, Any]]:
        """
        Загружает schemas.json и возвращает dict {tool_name: raw_schema}.
        Кешируется на инстанс.
        """
        if self._schemas is not None:
            return self._schemas
        path = self.schemas_path or _default_schemas_path()
        if not os.path.isfile(path):
            raise FileNotFoundError(f"schemas.json not found at {path!r}")
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError(
                f"schemas.json must be a dict, got {type(data).__name__}"
            )
        # sanity-check: все 6 схем должны быть
        missing = [n for n in executors.EXECUTORS if n not in data]
        if missing:
            log.warning(
                "schemas.json missing entries: %s (path=%s)",
                missing, path,
            )
        self._schemas = data
        return data

    def list_tool_names(self) -> List[str]:
        return list(self.load_schemas().keys())

    # ------------------------------------------------------------------
    # Format converters
    # ------------------------------------------------------------------
    def openai_tools(self) -> List[Dict[str, Any]]:
        """
        OpenAI-style `tools=[...]` (для совместимости с другими провайдерами).
        Каждая запись: {"type": "function", "function": {name, description, parameters}}.
        """
        out: List[Dict[str, Any]] = []
        for name, schema in self.load_schemas().items():
            out.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": schema.get("description", ""),
                    "parameters": _strip_additional_properties(schema.get("parameters", {})),
                },
            })
        return out

    def gemini_function_declarations(self) -> List[Dict[str, Any]]:
        """
        Готовый список `functionDeclarations` для payload["tools"]
        Google GenAI API.

        Совпадает с тем, что уже принимает `GoogleGeminiClient.chat`:
            payload["tools"] = [{"functionDeclarations": [...]}]
        """
        out: List[Dict[str, Any]] = []
        for name, schema in self.load_schemas().items():
            params = _strip_additional_properties(schema.get("parameters", {}))
            out.append({
                "name": name,
                "description": schema.get("description", ""),
                "parameters": params,
            })
        return out

    def gemini_tools_payload(self) -> List[Dict[str, Any]]:
        """Удобный враппер: сразу [{functionDeclarations: [...]}] для API."""
        return [{"functionDeclarations": self.gemini_function_declarations()}]

    # ------------------------------------------------------------------
    # Dispatching
    # ------------------------------------------------------------------
    async def run(
        self,
        name: str,
        arguments: Any,
    ) -> List[ToolOperationResult]:
        """
        Маршрутизирует вызов в Python-executor. Возвращает список
        `ToolOperationResult` (даже для single-result tools — это даёт
        единый формат, который легко сериализуется обратно в tool_response).
        """
        if self._lock is None:
            # asyncio.Lock создаётся внутри event loop, поэтому
            # инициализируем его лениво.
            self._lock = asyncio.Lock()
        async with self._lock:
            return await dispatch(
                name,
                arguments,
                workspace=self.workspace,
                auto_approve=self.auto_approve,
            )

    # ------------------------------------------------------------------
    # Tool response formatting
    # ------------------------------------------------------------------
    @staticmethod
    def format_tool_response(
        name: str,
        results: List[ToolOperationResult],
        *,
        max_chars: int = 48_000,
    ) -> str:
        """
        Превращает список результатов в строку, пригодную для отправки
        обратно в LLM как `tool_response` (или как `functionResponse` в Gemini).
        """
        if not results:
            return f"[{name}] no results"
        parts: List[str] = []
        for r in results:
            tag = "OK" if r.success else "ERROR"
            head = f"[{name}] {tag}: {r.query}"
            if r.error and not r.success:
                body = r.error
            else:
                body = r.result
            if isinstance(body, dict):
                # Например, image — выводим сериализованно
                body = json.dumps(body, ensure_ascii=False)
            elif body is None:
                body = ""
            if not isinstance(body, str):
                body = str(body)
            if len(body) > max_chars:
                head_size = max_chars // 2
                tail_size = max(1, max_chars - head_size)
                body = (
                    f"{body[:head_size]}\n"
                    f"[... truncated {len(body) - max_chars} chars ...]\n"
                    f"{body[-tail_size:]}"
                )
            parts.append(f"{head}\n{body}")
        return "\n\n".join(parts)


def _strip_additional_properties(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Рекурсивно убирает ключ `additionalProperties` из схемы.

    Зачем: Gemini strict-mode жалуется на `additionalProperties` в JSON-Schema.
    Все 6 схем из Cline уже без него, но мы страхуемся на случай, если
    когда-нибудь подсунут другую схему.
    """
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for k, v in schema.items():
        if k == "additionalProperties":
            continue
        if isinstance(v, dict):
            out[k] = _strip_additional_properties(v)
        elif isinstance(v, list):
            out[k] = [_strip_additional_properties(x) if isinstance(x, dict) else x
                      for x in v]
        else:
            out[k] = v
    return out
