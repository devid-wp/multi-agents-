# План развития Trinity (компактный, 3 фазы)

> Один вечер + день сделали 0.3.0 alpha. Дальше — 3 фазы, без растягивания.

## Фаза 1 — Стабилизация (сейчас, 1-2 недели) ✅ в работе
**Цель:** убрать острый долг, чтобы следующий агент не споткнулся.

- [x] Карта проекта `docs/PROJECT_MAP.md` (этот план — `docs/PLAN.md`)
- [x] Заменить все `print(DEBUG_*)` на `log.debug/warning` в `core/llm_clients.py:227-248,275-368,530,873` и `agents/base.py:146`
- [x] Уточнить `agents/manager.py:59` — убрать формулировку "ЖЁСТКИЙ ФОРС", оставить fallback на Ollama только когда `config is None`, документировать release-профиль
- [x] Задокументировать дубли `tools/` vs `trinity/tools/` vs `extracted_tools/` (в релизе активен только `tools/registry.py:34` — 5 инструментов) — см. docs/PROJECT_MAP.md:3
- [x] Задокументировать `threading.Lock` в `core/changes.py:8`/`core/rooms.py:7` (sync store + asyncio.to_thread — корректно)
- [x] Убрать магию `MAX_HISTORY=40` -> `settings.history_max_messages` в `core/config.py:90` + `core/history.py:58`

Критерий готовности: `grep -r "print(" core/ agents/ --include="*.py"` пусто, тесты `pytest -q` зеленые, карта актуальна.

## Фаза 2 — Закаливание (2-3 недели)
**Цель:** сделать alpha безопасной и предсказуемой.

- [ ] Аутентификация хотя бы local token (сейчас только `localhost_only` middleware `main.py:141`)
- [ ] Хранение: sqlite (aiosqlite) вместо JSON для `history/rooms/changes` — атомарность + конкурентность (сейчас `.tmp + os.replace`)
- [ ] Конфиг: вынести `MAX_HISTORY`, лимиты `workspace tree` в `core/config.py`; circuit breaker per-provider
- [ ] Разбить монолиты: `main.py:743` -> `routers/workspace.py`, `routers/diagnostics.py`; `ui/static/app.js:1314` -> модули
- [ ] CI: `ruff`, `mypy`, `pytest` в GitHub Actions

Критерий: один юзер не ломает сессии другого, рестарт не теряет `.trinity/`, CI зеленый.

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
