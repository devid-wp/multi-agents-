# Trinity — Бэкап и живучесть данных 0.8.0

## Где лежат данные

По умолчанию — SQLite `.trinity/trinity.db` внутри `WORKSPACE_DIR`:

```
$WORKSPACE_DIR/.trinity/trinity.db   # history, rooms, changes
$WORKSPACE_DIR/.trinity/              # создаётся при первом старте (core/db.py:init_db)
logs/trinity.log                      # RotatingFileHandler 5MB x3
```

**Фаза 8 — живучесть беты:** задайте `TRINITY_DATA_DIR` (см. `.env.example`) чтобы вынести данные вне workspace:

```bash
TRINITY_DATA_DIR=/home/user/.local/share/trinity  # или ./ .trinity-data
# тогда DB = $TRINITY_DATA_DIR/trinity.db
```

> Если `WORKSPACE_DIR=.` без `TRINITY_DATA_DIR` — БД в корне проекта. Добавьте `.trinity/` в `.gitignore` (уже есть) и не коммитьте её.

## Бэкап

```bash
WORKSPACE_DIR=.  # или ваш путь
# если TRINITY_DATA_DIR задан — DB в нём, иначе в $WORKSPACE_DIR/.trinity
DB="${TRINITY_DATA_DIR:-$WORKSPACE_DIR/.trinity}/trinity.db"

# Холодный бэкап (остановите trinity или используйте sqlite3 backup):
sqlite3 "$DB" ".backup '$HOME/trinity-backup-$(date +%F).db'"

# Или просто скопируйте файл когда сервис остановлен:
systemctl --user stop trinity  # если systemd
cp "$DB" "$HOME/trinity-backup-$(date +%F).db"
systemctl --user start trinity

# Проверка целостности:
sqlite3 "$DB" "PRAGMA integrity_check;"
```

## Восстановление

```bash
systemctl --user stop trinity
cp "$HOME/trinity-backup-2026-09-02.db" "$WORKSPACE_DIR/.trinity/trinity.db"
systemctl --user start trinity
```

## Миграции

`0.5.0` → SQLite-only (`core/db.py:migrate_json_if_needed` при `lifespan`). Старые JSON `.trinity/*.json` мигрируются один раз и игнорируются. `use_sqlite` deprecated (always True).

Смена `SESSION_SECRET` инвалидирует cookie `trinity_session` — история на диске сохранится, но браузер потребует повторный логин в Settings.

## Что не бэкапится

- `logs/trinity.log` — ротируется, не критичен.
- `dist/` и `node_modules/` — пересобираются `npm run build` / `npm ci`.
- `.env` — бэкапьте отдельно (секреты).
