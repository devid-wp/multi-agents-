# AGENT.md — Trinity продолжение

> Для следующего агента / для продолжения с этим же. Прочитай первым.

## Что это
Trinity `0.7.0` local alpha — FastAPI multi-agent `Planner → Critic → Executor`, `127.0.0.1` only, `WORKSPACE_DIR` sandbox, SSE `ProgressEvent`. 20 коммитов ahead `origin/main` (2026-09-01).

Карта: `docs/PROJECT_MAP.md:1`, план: `docs/PLAN.md:1`, чейнджлог: `CHANGELOG.md:1`.

## Стек (не добавлять новое без надобности)
- Backend: `FastAPI` + `Uvicorn` + `Pydantic` + `httpx/aiohttp` + `itsdangerous` + `watchfiles` + `aiosqlite` + `pytest-asyncio`/`respx`
- Frontend: `ui/` vanilla JS + `Tailwind` + `Vite` (`base /ui/` → `dist/`), `ui/static/modules/config|utils|sse.js` (ES-модули)
- Хранение: `SQLite` `.trinity/trinity.db` (`core/db.py`), JSON удалён в 0.6.0

Если нужная фича уже есть в стеке — используй его. **Не подключай новые фреймворки без надобности.**

## Структура (актуально 0.7.0)
```
main.py 116 строк (декомпозирован)
routers/workspace.py|diagnostics.py|rooms.py|changes.py|agents.py|chat.py|system.py
core/config|models|db|history|rooms|changes|diagnostics|session
agents/base|planner|critic|executor|manager
tools/file_tool|registry (6 tools: read/write/replace/delete/search/list + approval)
ui/index.html (ChatGPT OLED) + ui/static/app.js (ES modules) + styles.css + modules/*
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

## Что осталось
- Версии уже `0.7.0` (`main.py:65`, `package.json:3`), доки синхронизированы.
- `app.js` уже импортит `modules/config+utils`, осталось вычистить остатки дублей `trinity/tools` vs `tools`.
- Deprecated `/static` и `/chat` можно удалить после финального перехода на Vite.

Продолжай с `docs/PLAN.md` фаза 6.
