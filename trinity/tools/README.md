# trinity/tools — Cline bridge for Gemini (disabled in local alpha)

> В `0.7.1` локальный релиз использует только `tools/` (6 tools: read/write/replace/delete/search/list + approval). Этот пакет — опциональный мост для Gemini `functionCall` провайдеров.

## Что здесь

- `executors.py` — Python-порт 6 Cline-инструментов (`read_files`, `search_codebase`, `run_commands`, `fetch_web_content`, `editor`, `apply_patch`), sandbox через `workspace`.
- `manager.py` — `ClineToolManager`: грузит `schemas.json`, формирует `functionDeclarations` для Gemini, диспетчерит `functionCall` → `executors.dispatch`.
- `schemas.json` — mirror `extracted_tools/schemas.json` (эталон Cline-схем, без `additionalProperties` для Gemini strict mode).

## Связь с остальным

- `tools/` — **активный** реестр для всех провайдеров (OpenAI-compat, Ollama, NVIDIA). Интеграция через `tools/registry.py` → `ChangeStore` approval.
- `trinity/tools` — **неактивен** в local alpha (`agents/manager.py:cline_tool_manager = None`). Включается только если `AgentContext.cline_tool_manager` задан (Gemini-path в `agents/base.py:_dispatch_tool`).
- `extracted_tools/` — JS-референс исходных Cline tools (чистый Node, не исполняется в Python). `schemas.json` оттуда — эталон, `tools.js` — reference implementation.

## Когда трогать

- Новый Gemini-провайдер: править `executors.py` + `manager.py:gemini_function_declarations`.
- Новый локальный tool: править `tools/file_tool.py` + `tools/registry.py` (не этот пакет).
