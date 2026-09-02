# План развития Trinity (компактный, 3 фазы)

> Один вечер + день сделали 0.3.0 alpha. Дальше — 3 фазы, без растягивания.

## Фаза 1 — Стабилизация ✅ завершена (2026-08-31)
**Цель:** убрать острый долг, чтобы следующий агент не споткнулся.

- [x] Карта проекта `docs/PROJECT_MAP.md` (этот план — `docs/PLAN.md`)
- [x] Заменить все `print(DEBUG_*)` на `log.debug/warning` в `core/llm_clients.py:227-248,275-368,530,873` и `agents/base.py:146`
- [x] Уточнить `agents/manager.py:59` — убрать формулировку "ЖЁСТКИЙ ФОРС", оставить fallback на Ollama только когда `config is None`, документировать release-профиль
- [x] Задокументировать дубли `tools/` vs `trinity/tools/` vs `extracted_tools/` (в релизе активен только `tools/registry.py:34` — 5 инструментов) — см. docs/PROJECT_MAP.md:3
- [x] Задокументировать `threading.Lock` в `core/changes.py:8`/`core/rooms.py:7` (sync store + asyncio.to_thread — корректно)
- [x] Убрать магию `MAX_HISTORY=40` -> `settings.history_max_messages` в `core/config.py:90` + `core/history.py:58`

Критерий готовности: `grep -r "print(" core/ agents/ --include="*.py"` пусто, тесты `pytest -q` зеленые, карта актуальна.

## Фаза 2 — Закаливание ✅ завершена (часть 2, 2026-08-31)

- [x] Аутентификация: local token `TRINITY_LOCAL_TOKEN` / `settings.local_token` поверх `localhost_only` (`main.py:140`) — `/api/health` без токена
- [x] Хранение: sqlite backend `core/db.py` (`.trinity/trinity.db`, aiosqlite) + `settings.use_sqlite` + миграция из JSON для `history/rooms/changes` (`core/history.py:1`, `rooms.py:1`, `changes.py:1`)
- [x] Конфиг: лимиты вынесены — `history_max_messages`, `workspace_max_depth/max_entries`, `diagnostics_history_max`, `llm_circuit_breaker_threshold` (`core/config.py:90`)
- [x] Circuit breaker: глобальный -> per-provider `dict[_circuit_key]` (`core/llm_clients.py:44`)
- [x] Разбить монолиты: `main.py:519` -> `routers/workspace.py:163` + `routers/diagnostics.py:52`
- [x] UI модули: `ui/static/modules/config.js`, `utils.js`, `sse.js` созданы (совместимость сохранена)
- [x] CI: `.github/workflows/ci.yml` — ruff + mypy + `pytest -k "not real_api"`

Критерий: sqlite включается флагом `USE_SQLITE=1`, JSON остаётся fallback, `py_compile` ок.

## Фаза 3 — Продукт ✅ завершена (лето 2026)

- [x] Комнаты backend+UI: `PUT/DELETE /api/rooms/{id}` (`main.py:207`, `core/rooms.py`) + `✎/🗑` в `ui/index.html:58` + `app.js:renameRoom/deleteRoom`
- [x] Approval UX: `ui/static/app.js:renderDiff` + `styles.css` (diff-add/del/hunk/meta, copy button), `change-actions` стили
- [x] Сборка UI: `package.json` + `vite.config.js` + `tailwind.config.js` + `postcss.config.js` (CDN fallback сохранён, `npm run build` -> `dist/`)
- [x] Legacy: `main.py:/static` + `/chat` помечены `deprecated=True` (Vite /ui/ primary, будет удалено)
- [x] Наблюдаемость: `main.py:_check_rate_limit` (sliding window 60s, `chat_rate_limit_per_minute`) + `dt` + `est_tokens` лог в `event_stream finally`

## Фаза 4 — Vite & SQLite & Preview (0.5.0) ✅ 2026-09-01

- [x] SQLite default: `core/config.py:use_sqlite=True`, `main.py:lifespan` migrate, `USE_SQLITE` deprecated
- [x] Vite primary: `vite.config.js:base /ui/`, `ui/index.html` CDN удалён, `main.py:DIST_DIR` primary, `npm run build` ok
- [x] Workspace file: `GET /api/workspace/file?path=` (sandbox 50k) `routers/workspace.py:129`
- [x] Tests: `diagnostics_bus` patch для `routers/diagnostics`, `endpoint_url` print→log (caplog), pytest 91 passed

## Фаза 5 — Preview & Delete & SQLite-only & Split (0.6.0) ✅ 2026-09-01

- [x] UI file preview: `ui/index.html:file-preview` + `app.js:openFilePreview` клик по дереву
- [x] Delete approval: `core/changes.py:propose_delete` + `op=delete` + `core/db.py` `ALTER op`, `tools/registry.py:DeleteFile` (6 инструментов)
- [x] DB cleanup: `core/history|rooms|changes` только SQLite, JSON удалён, `use_sqlite` deprecated
- [x] Декомпоз `main.py` 576→116: `routers/rooms|changes|agents|chat|system` + `ui/static/modules/*` ready

Критерий: Фаза 5 done — preview работает, delete с approval, только SQLite, main декомпозирован, тесты зелёные.

## Фаза 6 — Cleanup OLED (0.7.1) ✅ 2026-09-02

- [x] SSE модули: `app.js` 1425→1270 `import {connectSSE,postSSE} from modules/sse.js` + linked AbortSignal (timeout + external abort)
- [x] Legacy удалён: `main.py:app.mount("/static")` + `routers/system.py:legacy_chat` (`/chat`) удалены, Vite `dist/` единственный источник
- [x] Tools docs: `trinity/tools/README.md` clarifies `tools/` (6 active, approval) vs `trinity/tools` (Gemini bridge, disabled `cline_tool_manager=None`) vs `extracted_tools` (JS ref)
- [x] Contract test: `tests/test_ui_release_contract.py` checks `sse.js` for timeout string
- [x] Versions: `package.json 0.7.1`, `main.py 0.7.1`, `CHANGELOG 0.7.1`, `PROJECT_MAP 0.7.1`

Критерий: `pytest -q` 91 passed, `npm run build` 7 modules, нет `deprecated` роутов.

## Фаза 7 — Boxed 0.8.0 ✅ 2026-09-02

- [x] CI strict: `ruff.toml` `select E/F ignore E402/E501`, `.github/workflows/ci.yml` без `|| true` (ruff+mypy 0 errors, autofix 18)
- [x] Systemd portable: `systemd/trinity.service` `__TRINITY_HOME__` template + `install.sh` `sed` substitution (no hardcoded `%h/Projects`)
- [x] Docs boxed: `docs/INSTALL.md` + `docs/BACKUP.md` (установка/обновление/бэкап `.trinity/trinity.db` + `scripts/build-release.sh`)
- [x] Artifact: `package.json 0.8.0`, `main.py 0.8.0`, `trinity-0.8.0.tar.gz` (189K, sha256), `.gitignore` boxed artifacts

Критерий: `pytest -q` 91 passed, `ruff check` 0, `mypy` 0, `npm run build` 7 modules, `build-release.sh` 189K tarball.

## Фаза 8 — Beta Viability (0.8.1) ✅ 2026-09-02

- [x] Data external: `core/config.py:data_dir` (`TRINITY_DATA_DIR`) + `core/db.py:_data_root()` — `.trinity` вне `WORKSPACE_DIR`, `main.py` log `db_path()`, `BACKUP.md` + `.env.example`
- [x] Backup API: `GET /api/backup` (FileResponse) + `GET /api/backup/integrity` (`PRAGMA integrity_check`) `routers/system.py:114`
- [x] Viability: `routers/workspace.py` ignore `.trinity/.trinity_sessions` (no DB leak), `core/diagnostics.py` `diagnostics_history_max` from settings
- [x] Versions: `package.json 0.8.1`, `main.py 0.8.1`, `CHANGELOG 0.8.1`, `PROJECT_MAP 0.8.1`

Критерий: `TRINITY_DATA_DIR=/tmp/test` → `db_path` external, `/api/backup` 200, `pytest 91 passed`, `ruff 0`, `mypy 0`.

## Фаза 9 — Public Beta Polish (0.9.0) ✅ 2026-09-02

- [x] Security: `main.py:66` headers `nosniff/DENY/no-referrer/no-store` + `GET /api/version` (`{"version": app.version}`) exempt from token
- [x] Cleanup: `static/` + `templates/` dirs removed (1228 deletions, Vite `dist/` only)
- [x] UI: `ui/index.html:57` version pill + `app.js:refreshVersion()` + `config.js` `version/backup` endpoints (Vite 31.14k)
- [x] Versions: `package.json 0.9.0`, `main.py 0.9.0`, `ui/index.html v0.9.0`, `CHANGELOG 0.9.0`

Критерий: `/api/version` 200 `v0.9.0`, headers `DENY`/`nosniff`, `pytest 91`, `npm build` 31.14k, no `static/`/`templates/`.

## Фаза 10 — 1.0.0 ✅ 2026-09-02

- [x] Final audit: `ruff 0` `mypy 0` `pytest 91/2` green, no `static/templates`, `GET /api/version` + `/api/backup/integrity` OK, headers nosniff
- [x] Bump: `package.json 1.0.0`, `main.py 1.0.0`, `ui/index.html v1.0.0`, `CHANGELOG 1.0.0` (boxed+beta+public beta → release)
- [x] Freeze: `docs/PROJECT_MAP.md` 1.0, `AGENT.md` 1.0, `README 1.0` — docs/PLAN phase 10, tags `v0.8.0` `v0.8.1` `v0.9.0` `v1.0.0`
- [x] Artifact: `scripts/build-release.sh 1.0.0` → `trinity-1.0.0.tar.gz` 190K + sha256, Vite 31.14k

Критерий: `1.0.0` boxed local release — 91 passed, ruff/mypy 0, no legacy, version pill, backup API, TRINITY_DATA_DIR viable.

---

### Что НЕ делаем
- Не добавляем новых агентов/провайдеров без нужды (стек frozen: FastAPI/Vite/Tailwind/SQLite/httpx)
- Не вводим multi-user изоляцию (single token `TRINITY_LOCAL_TOKEN`)
- Не удаляем `trinity/tools` (нужен для Gemini) и `extracted_tools` (JS reference) — только документируем

### Как работать следующему агенту
1. Прочитай `docs/PROJECT_MAP.md`
2. Возьми чекбокс из текущей фазы, сделай коммит
3. Обнови чекбокс в этом файле и закоммить
