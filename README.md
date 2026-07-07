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
├── core/                    # Ядро: конфиг, LLM-клиенты, сессии
│   ├── llm_clients.py       # NvidiaClient, OllamaClient, OpenAICompatibleClient
│   └── models.py            # Pydantic-модели (ChatRequest, strategy и др.)
├── agents/                  # Planner, Critic, Executor, Manager
│   └── manager.py           # run_task(strategy="auto|planner|direct")
├── tools/                   # Cline-подобные инструменты (file, bash)
├── ui/                      # Mission Control UI
│   ├── index.html
│   └── static/
│       ├── app.js           # Роутинг стратегий, SSE-обработчик
│       └── styles.css
├── static/ + templates/     # Legacy UI (обратная совместимость)
└── tests/                   # pytest (e2e)
```

---

## Быстрый старт — Windows

```powershell
# Запустить PowerShell в корне проекта

# Разрешить запуск скриптов (один раз)
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# Старт: создаст venv, поставит deps, запустит сервер
.\start.ps1

# Параметры:
.\start.ps1 -Port 9000          # другой порт
.\start.ps1 -SkipOllama         # не запускать Ollama
.\start.ps1 -NoDev              # production (без --reload)
```

Откройте `http://localhost:8000/ui/` и настройте провайдеры в ⚙️ Settings.

---

## Быстрый старт — Arch Linux / Linux

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
| `PLANNER_API_KEY`      | API-ключ Planner *(лок. разработка)*                         | в проде — через форму |
| `CRITIC_API_KEY`       | API-ключ Critic *(лок. разработка)*                          | в проде — через форму |
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

Все файловые операции **заперты внутри `WORKSPACE_DIR`**: при попытке выйти
через `../` или симлинк агент получит `[BLOCKED] Path '...' resolves outside workspace ...`.

---

## Тесты

```bash
source .venv/bin/activate
pytest -v
```

---

## Лицензия

MIT
