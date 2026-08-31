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

## Фаза 2 — Закаливание (2-3 недели) ✅ в работе (часть 1, 2026-08-31)

- [x] Аутентификация: local token `TRINITY_LOCAL_TOKEN` / `settings.local_token` поверх `localhost_only` (`main.py:140`) — `/api/health` без токена
- [ ] Хранение: sqlite (aiosqlite) вместо JSON для `history/rooms/changes` — отложено на часть 2 (сейчас `.tmp + os.replace` оставлен, добавлен коммент)
- [x] Конфиг: лимиты вынесены — `history_max_messages`, `workspace_max_depth/max_entries`, `diagnostics_history_max`, `llm_circuit_breaker_threshold` (`core/config.py:90`)
- [x] Circuit breaker: глобальный `_global_consecutive_errors` -> per-provider `dict[_circuit_key]` (`core/llm_clients.py:44`)
- [x] Разбить монолиты: `main.py:519` (было 743) -> `routers/workspace.py:163` + `routers/diagnostics.py:52`; лимиты через `settings`
- [x] UI модули: `ui/static/modules/config.js`, `utils.js`, `sse.js` созданы (будущий импорт в `app.js`, сохранена совместимость с `tests/test_ui_release_contract.py`)
- [x] CI: `.github/workflows/ci.yml` — ruff + mypy + `pytest -k "not real_api"` на Python 3.11

Критерий части 1: `py_compile` ок, `print` поиск пустой, `/api/workspace/*` и `/api/diagnostics/*` через routers.

Остаток Фазы 2 (часть 2): sqlite миграция + `app.js` полный переход на модули.

## Фаза 3 — Продукт (3-4 недели)
**Цель:** закрыть UX-дыры alpha.

- [ ] Комнаты: rename/delete в UI (сейчас только create/list `main.py:186`)
- [ ] Approval UX: diff для `replace_in_file`, отмена по `base_hash` уже есть `core/changes.py:77`
- [ ] Сборка UI: Vite, убрать Tailwind CDN, удалить legacy `templates/`/`static/`
- [ ] Лимиты и наблюдаемость: rate-limit LLM, логи `logs/trinity.log` ротация уже есть `main.py:78`, добавить costs/latency

Критерий: пользователь может создать/удалить комнату, увидеть дифф, собрать UI без CDN, деплой через `systemd/trinity.service` без ручных правок.

---

### Что НЕ делаем
- Не добавляем новых агентов/провайдеров до конца Фазы 1
- Не вводим multi-user изоляцию до Фазы 2 (sqlite)
- Не трогаем `extracted_tools/tools.js` до решения о судьбе `trinity/tools/`

### Как работать следующему агенту
1. Прочитай `docs/PROJECT_MAP.md`
2. Возьми чекбокс из текущей фазы, сделай коммит
3. Обнови чекбокс в этом файле и закоммить
