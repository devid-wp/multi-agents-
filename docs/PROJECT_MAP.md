# Trinity — Карта проекта (для следующего агента)

> Версия карты: 2026-09-02, кодовая база `1.0.0` local alpha. Читай этот файл первым.

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
- **Frontend:** `ui/` — vanilla JS + Tailwind (Vite build → `dist/`, `base /ui/`), CDN удалён в 0.5.0. Legacy `templates/index.html` + `static/` удалены в 1.0.0.
- **Хранение:** SQLite `core/db.py` `.trinity/trinity.db` (history/rooms/changes, JSON fallback удалён в 0.6.0)

## 3. Структура (что где)

```
main.py                 # entry-point 113 строк (было 743 → декомпозирован в routers/* в 0.6.0, /static удалён в 1.0.0)
routers/workspace.py    # GET /api/workspace/tree|file + /stream (watchfiles), лимиты из settings
routers/diagnostics.py  # GET /api/diagnostics/stream/history (SSE + ring buffer)
routers/rooms.py        # GET|POST /api/rooms + PUT|DELETE /api/rooms/{id}
routers/changes.py      # GET /api/changes + POST /api/changes/{id}/decision (op=write|delete)
routers/agents.py       # GET /api/agents/active + POST /api/agents/switch
routers/chat.py         # POST /api/chat (SSE, rate-limit) + GET /api/chat/history
routers/system.py       # GET|POST /api/settings + GET /api/health (legacy /chat удалён в 1.0.0)
core/config.py          # AppSettings (.env), UserCredentials (сессия), дефолты моделей/URL (use_sqlite deprecated)
core/models.py          # ChatMessage, ProgressEvent(kind=agent_start|agent_message|tool_call|tool_result|agent_done|final|strategy|error|info), AgentProviderConfig
core/llm_clients.py     # 5 клиентов, NvidiaProvider, retry, circuit breaker
core/db.py              # SQLite backend .trinity/trinity.db + migrate_json_if_needed, op column
core/history.py         # HistoryManager — SQLite only (sliding window 40)
core/rooms.py           # RoomStore — SQLite only, DEFAULT_ROOMS=[general]
core/changes.py         # ChangeStore — propose/propose_delete/diff/list/decide, base_hash защита
core/diagnostics.py     # DiagnosticsBus — deque 500 + fan-out по asyncio.Queue
core/session.py         # get_credentials / save_credentials / mask_key (itsdangerous)
agents/base.py          # Agent (SYSTEM_PROMPT, _call_llm, parse_json_tool_calls, _run_tools)
agents/planner.py       # PlannerAgent (meta-llama/llama-3-70b)
agents/critic.py        # CriticAgent (VERDICT: OK / REVISION)
agents/executor.py      # ExecutorAgent + _request_final_report
agents/manager.py       # AgentManager — run_task(strategy=auto|planner|direct)
tools/base.py           # абстрактный Tool
tools/file_tool.py      # ReadFile/WriteFile/ReplaceInFile/DeleteFile/SearchInFile/ListDir + _safe_resolve
tools/registry.py       # ToolRegistry — 6 инструментов (read/write/replace/delete/search/list), ChangeStore-интеграция
trinity/tools/          # Cline bridge для Gemini (disabled, см. trinity/tools/README.md), mirror extracted_tools/schemas.json
ui/index.html           # Mission Control layout + file-preview panel
ui/static/app.js        # ~1270 строк: bridge/workspace/settings (SSE из modules/sse.js)
ui/static/styles.css    # темы + .file-preview + diff стили
ui/static/modules/      # config.js/utils.js/sse.js (ES modules, app.js импортирует все 3)
tests/conftest.py       # env_sandbox, app_client, live_server_url
.env.example            # пример переменных (USE_SQLITE deprecated)
start.sh / start.ps1    # bootstrap скрипты
```

## 4. API (routers/*)

| Метод | Путь | Назначение | Router |
|-------|------|------------|--------|
| GET | `/` | 307 -> /ui/ | `main.py` |
| GET | `/ui/` | Mission Control (dist/ primary) | `main.py` |
| GET | `/api/settings` | маскированные настройки | `system.py` |
| POST | `/api/settings` | сохранить в cookie | `system.py` |
| POST | `/api/chat` | SSE стрим `ProgressEvent` | `routers/chat.py` |
| GET | `/api/diagnostics/stream` | глобальный SSE | `routers/diagnostics.py` |
| GET | `/api/diagnostics/history?limit=200` | кольцевой буфер | `diagnostics.py` |
| GET | `/api/workspace/tree?path=.&hidden=0` | снимок дерева | `routers/workspace.py` |
| GET | `/api/workspace/file?path=` | превью файла (sandbox, 50k) | `workspace.py` |
| GET | `/api/workspace/stream` | SSE watchfiles | `workspace.py` |
| GET | `/api/rooms` / POST `/api/rooms` | комнаты | `routers/rooms.py` |
| PUT | `/api/rooms/{id}` | rename | `rooms.py` |
| DELETE | `/api/rooms/{id}` | delete | `rooms.py` |
| GET | `/api/changes` | approval лист | `routers/changes.py` |
| POST | `/api/changes/{id}/decision` | approve/reject (write/delete) | `changes.py` |
| GET | `/api/agents/active` | активный агент | `routers/agents.py` |
| POST | `/api/agents/switch` | switch | `agents.py` |
| GET | `/api/chat/history?session_id=&room_id=` | восстановление bridge | `routers/chat.py` |
| GET | `/api/health` | ollama | `system.py` |

## 5. Потоки данных

**Chat:** `app.js:sendMessage -> postSSE -> routers/chat.py:chat -> AgentManager.run_task:141 -> planner.run -> critic loop -> executor.run -> ProgressEvent.to_sse -> StreamingResponse`

**Diagnostics:** `ToolRegistry.execute:155 -> diagnostics_bus.publish_tool_execution -> diagnostics_bus.publish (tool_call/result) -> routers/diagnostics.py:diagnostics_stream -> EventSource`

**Workspace:** `routers/workspace.py:_walk_workspace + awatch -> /api/workspace/stream -> app.js:applyWorkspaceChange` ; `GET /api/workspace/file -> file-preview panel`

**File approval:** `ToolRegistry.execute (write_file/replace_in_file/delete_file) -> ChangeStore.propose/propose_delete:69 (diff, base_hash, op) -> ToolResult "Proposal ID: ..." -> UI refreshChanges -> POST /decision -> ChangeStore.decide:137 (delete: unlink, write: atom write)`

## 6. Конфиг

Переменные (`core/config.py:44`): `SESSION_SECRET!`, `WORKSPACE_DIR=.`, `LLM_TIMEOUT_SECONDS=120`, `MAX_ITERATIONS=5`, `PLANNER/CRITIC/EXECUTOR_BASE_URL/MODEL/API_KEY`, `OLLAMA_URL`, `OPENROUTER_API_KEY`. Секреты — в signed cookie, не в `.env` в проде. `settings` — синглтон.

## 7. Состояние (1.0.0)

- **Готово (0.5.0):** SQLite default (`core/config.py:use_sqlite=True`, lifespan migrate), Vite dist primary (`vite.config.js:base /ui/`, `main.py:DIST_DIR`), `GET /api/workspace/file` (sandbox 50k), approval delete_file (`ChangeStore op=delete`), SQLite-only (JSON удалён).
- **Готово (0.6.0):** UI file preview (`ui/index.html:file-preview` + `app.js:openFilePreview`), `delete_file` с approval (6 инструментов), `main.py` 576→116 декомпозирован в `routers/*`, `core/history|rooms|changes` только SQLite, тесты 91 passed (py 3.14).
- **Готово (0.7.0):** ChatGPT OLED redesign, `app.js` ES modules `config/utils`, 3×1fr settings, Python 3.14 compat.
- **Готово (0.7.1):** `app.js` 1425→1270 импортирует `modules/sse.js` (linked AbortSignal), `main.py`/`routers/system.py` legacy `/static`+`/chat` удалены, `trinity/tools/README.md` clarifies roles, contract test modular.
- **Готово (0.8.0 boxed):** `ruff.toml` strict `E/F`, CI без `|| true`, systemd `__TRINITY_HOME__` portable, `docs/INSTALL.md`+`BACKUP.md`, `scripts/build-release.sh` 189K tarball.
- **Готово (0.8.1 beta):** `TRINITY_DATA_DIR` (`core/db.py:_data_root`), `GET /api/backup`+`/integrity` (`routers/system.py:114`), `workspace` ignore `.trinity`, `diagnostics_history_max` from settings.
- **Готово (0.9.0 public beta):** headers `nosniff/DENY`, `GET /api/version`, `static/templates` removed, version pill `Trinity v0.9.0` + `/api/version` fetch.
- **Готово (1.0.0):** boxed+beta → release, audit `ruff 0/mypy 0/91 passed`, no legacy, `trinity-1.0.0.tar.gz`, docs freeze.
- **Ограничения:** local alpha only, single token `TRINITY_LOCAL_TOKEN`, нет multi-user изоляции.
- **Долг:** `trinity/tools` (Gemini bridge, disabled) vs `tools/` (active 6) — документирован, `threading.Lock` корректен via sync store.

## 8. Новые модули Фазы 2-3
- `core/config.py:90` — `history_max_messages`, `workspace_max_depth/max_entries`, `llm_circuit_breaker_threshold`, `local_token`
- `core/llm_clients.py:44` — per-provider circuit breaker `_circuit_errors[key]`
- `routers/` — вынесено из `main.py`
- `ui/static/modules/` — `config.js`/`utils.js`/`sse.js` (будущий импорт, пока `app.js` совместим)
- `.github/workflows/ci.yml` — ruff/mypy/pytest

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
