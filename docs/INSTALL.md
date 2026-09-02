# Trinity — Установка коробки 0.8.0

> Локальная альфа, `127.0.0.1` only. Все файловые операции заперты в `WORKSPACE_DIR`.

## Быстрый старт (Arch Linux)

```bash
git clone <repo> ~/trinity && cd ~/trinity
chmod +x install.sh start.sh
./install.sh                # pacman + venv + pip + .env из .env.example
# или без системных пакетов:
./install.sh --no-system --yes
```

### Ручной запуск

```bash
source .venv/bin/activate
SESSION_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))") \
  uvicorn main:app --host 127.0.0.1 --port 8000 --reload
# open http://127.0.0.1:8000/ui/ → Settings → провайдеры → chat
```

### Systemd (автозапуск)

```bash
TRINITY_HOME="$(pwd)"
mkdir -p ~/.config/systemd/user
sed "s|__TRINITY_HOME__|$TRINITY_HOME|g" systemd/trinity.service > ~/.config/systemd/user/trinity.service
systemctl --user daemon-reload
systemctl --user enable --now trinity.service
sudo loginctl enable-linger $USER   # жить после logout
journalctl --user -u trinity -f
curl http://127.0.0.1:8000/api/health
```

## Windows

```powershell
git clone <repo> C:\Projects\trinity
cd C:\Projects\trinity
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
.\start.ps1                 # venv + deps + server на 8000
.\start.ps1 -Port 9000 -NoDev
```

## Сборка UI

```bash
npm ci
npm run build   # -> dist/ (Vite base /ui/)
npm run dev     # :5173 proxy /api -> :8000
```

## Проверка

```bash
pytest -q -k "not real_api"   # 91 passed, mocked LLM
ruff check core/ agents/ tools/ routers/ main.py
mypy core/ agents/ --ignore-missing-imports
```

## Обновление коробки

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
npm ci && npm run build
systemctl --user restart trinity  # если systemd
```

## Переменные окружения

См. `.env.example`. Минимум для коробки: `SESSION_SECRET` (32+ символов, `openssl rand -hex 32`). Остальное через UI Settings (cookie `trinity_session`).
