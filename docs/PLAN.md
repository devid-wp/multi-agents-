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

## Фаза 3 — Продукт (3-4 недели) ✅ в работе (старт)

- [x] Комнаты backend: `PUT /api/rooms/{id}` rename + `DELETE /api/rooms/{id}` (`main.py:207`, `core/rooms.py:rename/delete`) — UI кнопки next
- [ ] Approval UX: diff для `replace_in_file` уже есть (`ui/static/app.js:816`, `core/changes.py:77`), нужно подсветка в UI
- [ ] Сборка UI: Vite, убрать Tailwind CDN, удалить legacy `templates/`/`static/` — отложено
- [ ] Лимиты и наблюдаемость: rate-limit LLM, costs/latency — отложено

Критерий: `PUT/DELETE /api/rooms` работают (sqlite + JSON), следующий шаг — UI для rename/delete.

---

### Что НЕ делаем
- Не добавляем новых агентов/провайдеров до конца Фазы 1
- Не вводим multi-user изоляцию до Фазы 2 (sqlite)
- Не трогаем `extracted_tools/tools.js` до решения о судьбе `trinity/tools/`

### Как работать следующему агенту
1. Прочитай `docs/PROJECT_MAP.md`
2. Возьми чекбокс из текущей фазы, сделай коммит
3. Обнови чекбокс в этом файле и закоммить
