# Changelog

## 0.6.0 - 2026-09-01

- UI file preview: `ui/index.html:file-preview` + `ui/static/app.js:openFilePreview` (click tree → `GET /api/workspace/file`)
- Delete approval: `core/changes.py:propose_delete` (`op=delete`) + `core/db.py:ALTER op` + `tools/registry.py:DeleteFile` (6 tools now), approval UX reused
- DB: SQLite-only — `core/history|rooms|changes` JSON fallback removed, `core/config:use_sqlite` deprecated (always True)
- Refactor: `main.py` 576→116 split into `routers/rooms|changes|agents|chat|system` + existing `workspace|diagnostics`; `ui/static/modules/*` ready
- Docs: `docs/PROJECT_MAP.md` 0.3.0→0.6.0, `docs/PLAN.md` phases 4+5

## 0.5.0 - 2026-09-01

- SQLite default: `core/config:use_sqlite=True` (was `False`), `main.py:lifespan` `init_db+migrate_json_if_needed`, `.env.example:USE_SQLITE`
- Vite primary: `vite.config.js:base /ui/`, `ui/index.html` CDN removed (`./static/*` relative), `main.py:DIST_DIR` primary with `ui/` fallback, `ui/static/styles.css` gradient fix, `package.json 0.5.0`, `npm run build` 16KB html + assets
- Workspace file: `GET /api/workspace/file?path=` (sandbox `_safe_resolve`, 50k truncate) `routers/workspace.py`
- Tests: fix `diagnostics_bus` patch for `routers/diagnostics`, `endpoint_url` DEBUG/WARNING `print`→`log` (caplog), pytest 91 passed on Python 3.14
- UI: Vite build verified `dist/index.html` `src="/ui/assets/..."`

## 0.4.0 - 2026-08-31

- Added `docs/PROJECT_MAP.md` and compact 3-phase plan `docs/PLAN.md`
- Hardening: per-provider circuit breaker, `TRINITY_LOCAL_TOKEN` auth, config-driven limits (`history_max_messages`, `workspace_max_depth/max_entries`)
- Split `main.py` 743->559 lines into `routers/workspace.py` + `routers/diagnostics.py`
- Modularized UI: `ui/static/modules/{config,utils,sse}.js` + Vite build (`package.json`/`vite.config.js`/`tailwind.config.js`)
- SQLite backend `core/db.py` (`.trinity/trinity.db`) with JSON fallback and `use_sqlite` flag for `history/rooms/changes`
- Rooms: `PUT/DELETE /api/rooms/{id}` + UI ✎/🗑 rename/delete (builtin guard)
- Approval UX: colored diff (`diff-add/del/hunk`) + copy button in `change-proposals`
- Observability: rate-limit `20/min` for `/api/chat` + `dt`/`est_tokens` latency log, legacy `/chat`/`/static` marked deprecated
- CI: `.github/workflows/ci.yml` (ruff/mypy/pytest)

## 0.3.0 - 2026-07-26

- Added persistent room-scoped chats with one built-in General room.
- Added Ollama readiness and missing-model guidance in the UI.
- Restored per-agent provider selection instead of forcing Ollama.
- Added diff preview and explicit approve/reject flow for file writes.
- Added a real HTTP/SSE workspace watcher test and normalized Windows paths.
- Restricted the release server to localhost and disabled Bash, Git and delete tools.
- Replaced the Windows bootstrap with a deterministic Python 3.11+ installer.
- Added room creation/switching and visible room-scoped history to the UI.

### Known limitations (0.4.0)

- Local alpha only; token auth is single shared `TRINITY_LOCAL_TOKEN`, no multi-user isolation.
- SQLite is opt-in (`USE_SQLITE=1`), JSON remains default (will switch in 0.5.0).
- Vite build exists but CDN fallback remains; legacy `/chat` still mounted as deprecated.

### Known limitations (0.3.0)

- Local alpha only; no authentication or multi-user isolation.
- Rooms have create/list UI but no rename/delete UI yet.
- Session and proposal state is stored as local JSON, not a concurrent database.
