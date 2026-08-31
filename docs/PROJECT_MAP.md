# Trinity — Карта проекта (для следующего агента)

> Версия карты: 2026-08-31, кодовая база `0.3.0` local alpha. Читай этот файл первым.

## 1. Что это

Trinity — локальная multi-agent система на FastAPI: три агента совместно решают задачи разработки **планируют → критикуют → исполняют**. Работает только на `127.0.0.1`, все файловые операции заперты в `WORKSPACE_DIR`.

```
Пользователь -> POST /api/chat (strategy) -> AgentManager.run_task -> SSE ProgressEvent
  strategy=planner : Planner -> Critic -> final (без Executor)
  strategy=auto    : Planner -> Critic loop (max_iterations=5) -> Executor -> final
  strategy=direct  : Executor напрямую
```

## 2. Стек

- **Backend:** FastAPI 0.115 + Uvicorn, Pydantic 2.9, httpx/aiohttp, itsdangerous (cookie), watchfiles (workspace SSE)
- **LLM:** `core/llm_clients.py` — `OpenAICompatibleClient` база + `NvidiaClient` / `OllamaClient` / `OpenRouterClient` / `GoogleGeminiClient` / `AnthropicClient`. Retry+circuit breaker `with_retry_and_circuit_breaker`.
- **Frontend:** `ui/` — vanilla JS + Tailwind CDN, без сборки. Legacy `templates/index.html` + `static/` оставлены для совместимости.
- **Хранение:** JSON-файлы в workspace: `.trinity_sessions/` (история), `.trinity/rooms.json`, `.trinity/changes.json`

## 3. Структура (что где)

```
main.py                 # entry-point, все эндпоинты, lifespan, localhost_only middleware, workspace walker
core/config.py          # AppSettings (.env), UserCredentials (сессия), дефолты моделей/URL
core/models.py          # ChatMessage, ProgressEvent(kind=agent_start|agent_message|tool_call|tool_result|agent_done|final|strategy|error|info), AgentProviderConfig
core/llm_clients.py     # 5 клиентов, NvidiaProvider, retry, circuit breaker
core/history.py         # HistoryManager — load/save + sliding window 40
core/rooms.py           # RoomStore — комнаты, DEFAULT_ROOMS=[general], session_id = "{client}--room-{id}"
core/changes.py         # ChangeStore — propose/diff/list/decide, base_hash защита
core/diagnostics.py     # DiagnosticsBus — deque 500 + fan-out по asyncio.Queue, kinds=tool_call|tool_result|error|tool_execution
core/session.py         # get_credentials / save_credentials / mask_key (itsdangerous)
agents/base.py          # Agent (SYSTEM_PROMPT, _call_llm, parse_json_tool_calls, _run_tools, _dispatch_tool, run loop)
agents/planner.py       # PlannerAgent (meta-llama/llama-3-70b)
agents/critic.py        # CriticAgent (вердикт VERDICT: OK / REVISION)
agents/executor.py      # ExecutorAgent + _request_final_report (отдельный LLM-вызов для отчета)
agents/manager.py       # AgentManager — создает 3 клиентов, readiness_report, run_task
tools/base.py           # абстрактный Tool
tools/file_tool.py      # ReadFile/WriteFile/ReplaceInFile/DeleteFile/SearchInFile/ListDir + _safe_resolve (sandbox)
tools/registry.py       # ToolRegistry — 5 инструментов в релизе (read/write/replace/search/list), ChangeStore-интеграция
tools/bash_tool.py etc  # выключены в релизе (не регистрируются)
trinity/tools/          # дублирующий порт 6 cline-инструментов (через cline_tool_manager в AgentContext) — в релизе не активен
ui/index.html           # Mission Control layout (topbar + workspace + bridge + composer)
ui/static/app.js        # 1314 строк: SSE (connectSSE/postSSE), settings modal, bridge render, workspace tree
ui/static/styles.css    # переменные темы
tests/conftest.py       # env_sandbox autouse, app_client (ASGI), live_server_url (uvicorn thread) для SSE
.env.example            # пример переменных
start.sh / start.ps1    # bootstrap скрипты
```

## 4. API (main.py)

| Метод | Путь | Назначение |
|-------|------|------------|
| GET | `/` | 307 -> /ui/ |
| GET | `/ui/` | Mission Control (StaticFiles html=True) |
| GET | `/chat/` | legacy UI |
| GET | `/api/settings` | маскированные настройки |
| POST | `/api/settings` | сохранить в signed cookie `trinity_session` (30д) |
| POST | `/api/chat` | SSE стрим `ProgressEvent` (центральный) |
| GET | `/api/diagnostics/stream` | глобальный SSE tool_call/tool_result/error |
| GET | `/api/diagnostics/history?limit=200` | кольцевой буфер |
| GET | `/api/workspace/tree?path=.&hidden=0` | снимок дерева (depth 4, 1000 entries) |
| GET | `/api/workspace/stream` | SSE watchfiles |
| GET | `/api/rooms` / POST `/api/rooms` | комнаты |
| GET | `/api/changes` / POST `/api/changes/{id}/decision` | approval диффов |
| GET | `/api/agents/active` / POST `/api/agents/switch` | активный агент (UI) |
| GET | `/api/chat/history?session_id=&room_id=` | восстановление bridge |
| GET | `/api/health` | ollama available/model_installed |

## 5. Потоки данных

**Chat:** `app.js:sendMessage -> postSSE -> main.py:chat:399 -> AgentManager.run_task:141 -> planner.run -> critic loop -> executor.run -> ProgressEvent.to_sse -> StreamingResponse`

**Diagnostics:** `ToolRegistry.execute:155 -> diagnostics_bus.publish_tool_execution -> diagnostics_bus.publish (tool_call/result) -> main.py:diagnostics_stream:521 -> EventSource в app.js`

**Workspace:** `main.py:_walk_workspace:592 + awatch -> /api/workspace/stream:687 -> app.js:applyWorkspaceChange`

**File approval:** `ToolRegistry.execute (write_file/replace_in_file) -> ChangeStore.propose:39 (diff, base_hash) -> ToolResult "Proposal ID: ..." -> UI refreshChanges -> POST /decision -> ChangeStore.decide:66 (проверка base_hash, атомарная запись)`

## 6. Конфиг

Переменные (`core/config.py:44`): `SESSION_SECRET!`, `WORKSPACE_DIR=.`, `LLM_TIMEOUT_SECONDS=120`, `MAX_ITERATIONS=5`, `PLANNER/CRITIC/EXECUTOR_BASE_URL/MODEL/API_KEY`, `OLLAMA_URL`, `OPENROUTER_API_KEY`. Секреты — в signed cookie, не в `.env` в проде. `settings` — синглтон.

## 7. Состояние (0.3.0)

- **Готово:** 3 агента, 3 стратегии, SSE, sandbox, approval, комнаты (general builtin), история с sliding window, workspace watcher, diagnostics bus, healthcheck, Mission Control UI.
- **Ограничения (CHANGELOG):** local alpha only, нет auth/multi-user, нет rename/delete комнат в UI, JSON без конкурентности, localhost only.
- **Долг (фаза 1):** DEBUG `print` в `llm_clients.py`/`base.py`, жесткий комментарий `manager.py:59` "ФОРС ОЛЛАМЫ" (фактически — fallback), дубли `tools` vs `trinity/tools`, `threading.Lock` вместо `asyncio.Lock`, монолит `main.py:743`/`app.js:1314`.

## 8. Как запустить / проверить

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
SESSION_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))") \
  uvicorn main:app --host 127.0.0.1 --port 8000 --reload
# open http://127.0.0.1:8000/ui/ -> Settings -> провайдеры -> chat
pytest -q              # mocked, без LLM
pytest -q -k "not real_api"
```

## 9. Куда смотреть при изменениях

- Новый инструмент: `tools/file_tool.py` + `tools/registry.py:_register_defaults` + `main.py` whitelist, тесты `tests/test_tools.py`
- Новый LLM-провайдер: `core/llm_clients.py` (наследуй BaseLLMClient) + `core/config.py` + `agents/manager.py:build_client` + `ui/static/app.js` settings modal
- Новый эндпоинт: `main.py` + `core/models.py` + `ui/static/app.js:ENDPOINTS`
- Миграция хранения: `core/history.py` / `rooms.py` / `changes.py` (атомарность через `.tmp + os.replace`)

## 10. План (см. docs/PLAN.md)

Фаза 1 Стабилизация -> Фаза 2 Закаливание -> Фаза 3 Продукт. Детально в `docs/PLAN.md`.
