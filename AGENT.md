# AGENT.md — Trinity продолжение

> Для следующего агента / для продолжения с этим же. Прочитай первым.

## Что это
Trinity `0.7.1` local alpha — FastAPI multi-agent `Planner → Critic → Executor`, `127.0.0.1` only, `WORKSPACE_DIR` sandbox, SSE `ProgressEvent`. 23 коммита ahead `origin/main` (2026-09-02).

Карта: `docs/PROJECT_MAP.md:1`, план: `docs/PLAN.md:1`, чейнджлог: `CHANGELOG.md:1`.

## Стек (не добавлять новое без надобности)
- Backend: `FastAPI` + `Uvicorn` + `Pydantic` + `httpx/aiohttp` + `itsdangerous` + `watchfiles` + `aiosqlite` + `pytest-asyncio`/`respx`
- Frontend: `ui/` vanilla JS + `Tailwind` + `Vite` (`base /ui/` → `dist/`), `ui/static/modules/config|utils|sse.js` (ES-модули, app.js 1270 строк)
- Хранение: `SQLite` `.trinity/trinity.db` (`core/db.py`), JSON удалён в 0.6.0

Если нужная фича уже есть в стеке — используй его. **Не подключай новые фреймворки без надобности.**

## Структура (актуально 0.7.1)
```
main.py 113 строк (декомпозирован, /static удалён)
routers/workspace.py|diagnostics.py|rooms.py|changes.py|agents.py|chat.py|system.py (legacy /chat удалён)
core/config|models|db|history|rooms|changes|diagnostics|session
agents/base|planner|critic|executor|manager (cline_tool_manager=None — local alpha)
tools/file_tool|registry (6 tools: read/write/replace/delete/search/list + approval)
trinity/tools/ (Gemini Cline bridge, disabled — см. trinity/tools/README.md)
ui/index.html (ChatGPT OLED) + ui/static/app.js 1270 (ES modules config/utils/sse) + styles.css + modules/*
```

## Правила продолжения (от владельца — строго)
1. **Не делай костыли.** Старые фичи не подпираем хаками. `новое значит новое` — если тянем новую фичу, делаем честно, без `if legacy` заплаток.
2. **Не подключай новые фреймворки без надобности.** Если в нынешнем стеке уже есть решение — используй его (`FastAPI`, `Vite`, `Tailwind`, `SQLite`, `httpx`).
3. Каждый пункт — отдельным коммитом (`git commit -m "feat: ..."`), без `co-author`, без force-push.
4. Перед кодом — читай `docs/PROJECT_MAP.md`, после — `pytest -q` + `npm run build` должны быть зелёные.
5. `SESSION_SECRET` ≥32 символов, `localhost_only` middleware, `TRINITY_LOCAL_TOKEN` опционально.

## Как запустить / проверить
```bash
cd /home/kr1m12/Desktop/multi-agents-
source /tmp/trinity_venv/bin/activate
SESSION_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))") \
  python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
# http://127.0.0.1:8000/ui/
pytest -q
npm run build
```

## Что осталось (на 0.7.1 — фаза 6 done)
- Версии `0.7.1` (`main.py:65`, `package.json:3`), доки синхронизированы.
- `app.js` импортит `modules/config+utils+sse` (1425→1270).
- Deprecated `/static` и `/chat` удалены (Vite `dist/` primary).
- `trinity/tools` vs `tools` — документирован (`trinity/tools/README.md`).

Следующий шаг — фаза 7 по `docs/PLAN.md` (если появится) или фичи из бэклога.
