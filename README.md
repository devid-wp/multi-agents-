# Trinity — Multi-Agent System for autonomous development

Мульти-агентная система на FastAPI, состоящая из трёх специализированных агентов,
которые совместно решают задачи разработки: **планируют → критикуют → исполняют**.

| Агент         | Провайдер (по умолчанию)                            | Роль                                   |
|---------------|------------------------------------------------------|----------------------------------------|
| PlannerAgent  | NVIDIA NIM: `abacusai/dracarys-llama-3.1-70b-instruct` | Стратегическое планирование           |
| CriticAgent   | NVIDIA NIM: `google/gemma-2-27b-it`                  | Ревью и отбраковка планов              |
| ExecutorAgent | Ollama / NVIDIA / OpenAI-compatible (настраивается)  | Исполнение кода и работа с файлами    |

Каждый агент конфигурируется **независимо**: свой провайдер, API-ключ, base URL и имя модели.
Все агенты связаны через `AgentManager`; ExecutorAgent работает в **песочнице**
(`tools/file_tool.py` блокирует выход за пределы `WORKSPACE_DIR` через `Path.resolve()`).

---

## Структура проекта

```
multi_agents/
├── main.py                  # FastAPI entry-point
├── start.ps1                # 🪟 Windows PowerShell — быстрый старт
├── start.sh                 # 🐧 Arch/Linux — быстрый старт
├── install.sh               # Установщик для Arch Linux (pacman + venv)
├── requirements.txt
├── systemd/
│   └── trinity.service      # Пользовательский systemd-юнит
├── core/                    # Ядро: конфиг, LLM-клиенты, сессии, история
│   ├── llm_clients.py       # NvidiaClient, OllamaClient, OpenAICompatibleClient, GoogleGeminiClient
│   ├── history.py           # HistoryManager — JSON-персистентность диалогов
│   └── models.py            # Pydantic-модели (ChatRequest, strategy и др.)
├── agents/                  # Planner, Critic, Executor, Manager
│   └── manager.py           # run_task(strategy="auto|planner|direct")
├── tools/                   # Cline-подобные инструменты (file, bash, git)
│   └── git_tool.py          # execute_git — безопасный git с whitelist-подкомандами
├── ui/                      # Mission Control UI
│   ├── index.html
│   └── static/
│       ├── app.js           # Роутинг стратегий, SSE-обработчик, session persistence
│       └── styles.css
├── static/ + templates/     # Legacy UI (обратная совместимость)
└── tests/                   # pytest (e2e)
```

---

## 🪟 Быстрый старт — Windows

### Требования

| Компонент | Минимальная версия | Где скачать |
|-----------|-------------------|-------------|
| Python    | 3.11+             | [python.org](https://www.python.org/downloads/) — **обязательно** ставить с галочкой «Add to PATH» |
| Git       | любая             | [git-scm.com](https://git-scm.com/download/win) |
| Ollama *(опционально, для локального LLM)* | 0.3+ | [ollama.com](https://ollama.com/download/windows) |

### Установка и запуск (рекомендуется)

```powershell
# 1. Клонировать репозиторий
git clone <repo-url> C:\Projects\multi_agents
cd C:\Projects\multi_agents

# 2. Разрешить выполнение скриптов (один раз, для текущего пользователя)
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# 3. Запустить — создаст venv, поставит зависимости, стартует сервер
.\start.ps1

# Дополнительные параметры:
.\start.ps1 -Port 9000          # другой порт (по умолчанию 8000)
.\start.ps1 -SkipOllama         # не запускать / не проверять Ollama
.\start.ps1 -NoDev              # production-режим (без --reload)
```

Откройте `http://localhost:8000/ui/` и настройте провайдеры в ⚙️ **Settings**.

### Установка вручную (без скрипта)

```powershell
# Создать виртуальное окружение
python -m venv .venv
.\.venv\Scripts\activate

# Установить зависимости
pip install -r requirements.txt

# Задать секрет сессии (ОБЯЗАТЕЛЬНО сменить в проде)
$env:SESSION_SECRET = "my-super-secret-change-me"

# Запустить сервер
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

### Ollama на Windows

```powershell
# После установки Ollama скачать модель для Executor:
ollama pull qwen2.5-coder:7b

# Проверить, что Ollama работает:
Invoke-RestMethod http://localhost:11434/api/tags
```

### Автозапуск при входе (Task Scheduler)

Windows не поддерживает systemd. Используй «Планировщик задач»:

```powershell
$action  = New-ScheduledTaskAction `
             -Execute "powershell.exe" `
             -Argument "-WindowStyle Hidden -File C:\Projects\multi_agents\start.ps1 -NoDev" `
             -WorkingDirectory "C:\Projects\multi_agents"
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0

Register-ScheduledTask -TaskName "Trinity Agent" `
  -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest
```

### Файл `.env` на Windows

Создай `.env` в корне проекта (читается через `python-dotenv` автоматически):

```env
SESSION_SECRET=change-me-in-production
WORKSPACE_DIR=C:\Projects\my_project
MAX_ITERATIONS=3
PLANNER_API_KEY=nvapi-...
CRITIC_API_KEY=nvapi-...
```

Или задай переменные в PowerShell-сессии перед запуском:

```powershell
$env:SESSION_SECRET = "change-me"
$env:WORKSPACE_DIR  = "C:\Projects\my_project"
.\start.ps1 -NoDev
```

> ⚠️ `.env` добавлен в `.gitignore`. Не коммить файл с реальными ключами.

### Типичные проблемы на Windows

| Проблема | Решение |
|----------|---------|
| `python не является командой` | Установи Python с галочкой «Add to PATH», или используй `py` вместо `python` |
| `Execution Policy` ошибка при запуске `.ps1` | `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` |
| Порт 8000 занят | `.\start.ps1 -Port 9000` или: `netstat -ano | findstr :8000`, затем `taskkill /PID <pid> /F` |
| Ollama не отвечает | Убедись, что Ollama запущена (иконка в трее), или запусти `ollama serve` в отдельном терминале |
| `uvloop` ошибка при импорте | Нормально — uvloop только для Linux, на Windows asyncio работает нативно |
| `git` не найден в `execute_git` | Установи [Git for Windows](https://git-scm.com/download/win) и перезапусти терминал |
| Проблемы с кодировкой (кириллица в логах) | Добавь в начало PowerShell: `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8` |

---

## 🐧 Быстрый старт — Arch Linux / Linux

```bash
# 1. Клонируем
git clone <repo> ~/Projects/multi_agents
cd ~/Projects/multi_agents
chmod +x start.sh
./start.sh             # создаст venv, депс, запустит сервер

# Или через systemd:
cp systemd/trinity.service ~/.config/systemd/user/
systemctl --user enable --now trinity.service
sudo loginctl enable-linger $USER    # жить после logout
```

Откройте `http://127.0.0.1:8000/ui/` и настройте провайдеры в ⚙️ Settings.

---

## Стратегии маршрутизации

Выбор режима — кнопками в топбаре UI или полем `strategy` в API:

| Кнопка | `strategy` | Режим |
|---------|------------|-------|
| 🧠 **Planner** | `planner` | Только план: Planner → Critic. Executor **не** запускается. |
| ⚡ **Critic** | `auto` | Полный цикл: Plan → Review → Execute (дефолт). |
| ⚙️ **Executor** | `direct` | В обход Planner/Critic — задача сразу к Executor. |

```json
// POST /api/chat
{ "message": "Напиши hello.py", "strategy": "direct" }
```

---

## Переменные окружения

Полный список — в [`.env.example`](.env.example). Кратко:

| Переменная             | Назначение                                              | Обязательность |
|------------------------|---------------------------------------------------------|----------------|
| `SESSION_SECRET`       | Секрет подписи cookie (itsdangerous)                    | **обязательно сменить в проде** |
| `WORKSPACE_DIR`        | Песочница ExecutorAgent (sandbox)                        | опционально (`.`) |
| `LLM_TIMEOUT_SECONDS`  | Таймаут HTTP-запросов к LLM                              | опционально (`120`) |
| `MAX_ITERATIONS`       | Макс. итераций Planner↔Critic                            | опционально (`5`) |
| `PLANNER_BASE_URL`     | Базовый URL для Planner (NVIDIA NIM / OpenAI-compat)     | опционально |
| `CRITIC_BASE_URL`      | Базовый URL для Critic                                   | опционально |
| `EXECUTOR_BASE_URL`    | Базовый URL для Executor (Ollama / NVIDIA / любой)       | опционально |
| `PLANNER_MODEL`        | Имя модели Planner                                       | опционально |
| `CRITIC_MODEL`         | Имя модели Critic                                        | опционально |
| `EXECUTOR_MODEL`       | Имя модели Executor                                      | опционально |
| `PLANNER_API_KEY`      | API-ключ Planner *(лок. разработка)*                     | в проде — через форму |
| `CRITIC_API_KEY`       | API-ключ Critic *(лок. разработка)*                      | в проде — через форму |
| `OLLAMA_URL`           | Адрес локального Ollama                                  | опционально (`http://localhost:11434`) |

⚠️ **Безопасность**: `.env` лежит в `.gitignore` и не должен попадать в репозиторий.
В продакшне API-ключи рекомендуется вводить через UI — они хранятся в
подписанной сессии и не логируются.

---

## Инструменты ExecutorAgent

| Инструмент     | Назначение                                  |
|----------------|---------------------------------------------|
| `execute_bash` | Запуск shell-команд (с песочницей)          |
| `read_file`    | Чтение файла (UTF-8, truncate 50k символов) |
| `write_file`   | Создание / перезапись файла                 |
| `list_dir`     | Листинг директории (с размерами)            |
| `execute_git`  | Безопасные git-операции (whitelist)         |

Все файловые операции **заперты внутри `WORKSPACE_DIR`**: при попытке выйти
через `../` или симлинк агент получит `[BLOCKED] Path '...' resolves outside workspace ...`.

Разрешённые `execute_git` подкоманды: `status`, `diff`, `add`, `commit`, `push`,
`pull`, `checkout`, `log`, `branch`, `reset`, `restore`, `rm`, `mv`, `fetch`, `stash`, `show`, `tag`.

---

## Тесты

```bash
# Linux / macOS
source .venv/bin/activate
pytest -v

# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
pytest -v

# Только mocked-тесты (без реального LLM):
pytest -v -k "not real_api"

# Real-API smoke (требует PLANNER_API_KEY / OLLAMA_URL в env):
pytest -v -k "real_api"
```

---

## Лицензия

MIT
