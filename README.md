# Trinity — Multi-Agent System for autonomous development

Мульти-агентная система на FastAPI, состоящая из трёх специализированных агентов,
которые совместно решают задачи разработки: **планируют → критикуют → исполняют**.

| Агент        | Модель                                          | Роль                            |
|--------------|-------------------------------------------------|---------------------------------|
| PlannerAgent | NVIDIA: `abacusai/dracarys-llama-3.1-70b-instruct` | Стратегическое планирование    |
| CriticAgent  | NVIDIA: `google/gemma-2-27b-it`                 | Ревью и отбраковка планов        |
| ExecutorAgent| Ollama: `qwen2.5-coder`                          | Исполнение кода и работа с файлами |

Все агенты связаны через `AgentManager`, и ExecutorAgent работает в **песочнице**
(`tools/file_tool.py` блокирует выход за пределы `WORKSPACE_DIR` через `Path.resolve()`).

---

## Структура проекта

```
multi_agents/
├── main.py                  # FastAPI entry-point
├── install.sh               # Установщик для Arch Linux (pacman + venv)
├── requirements.txt
├── systemd/
│   └── trinity.service      # Пользовательский systemd-юнит
├── core/                    # Ядро: конфиг, LLM-клиенты, сессии
├── agents/                  # Planner, Critic, Executor, Manager
├── tools/                   # Cline-подобные инструменты (file, bash)
├── static/ + templates/     # UI (ChatGPT-подобный чат)
└── tests/                   # pytest
```

---

## Быстрый старт (Arch Linux)

```bash
# 1. Клонируем и ставим зависимости
git clone <repo> ~/Projects/multi_agents
cd ~/Projects/multi_agents
chmod +x install.sh
./install.sh                 # sudo pacman + venv + .env

# 2. (опционально) редактируем .env — но в проде ключи идут через форму
$EDITOR .env

# 3. Включаем пользовательский сервис systemd
mkdir -p ~/.config/systemd/user
cp systemd/trinity.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now trinity.service

# 4. Чтобы демон жил после logout
sudo loginctl enable-linger $USER

# 5. Проверка
systemctl --user status trinity
journalctl --user -u trinity -f
curl http://127.0.0.1:8000/api/health
```

Откройте `http://127.0.0.1:8000` и введите API-ключи NVIDIA в форме настроек.

### Альтернатива: запуск вручную

```bash
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## Переменные окружения

Полный список — в [`.env.example`](.env.example). Кратко:

| Переменная             | Назначение                                              | Обязательность |
|------------------------|---------------------------------------------------------|----------------|
| `SESSION_SECRET`       | Секрет подписи cookie (itsdangerous)                    | **обязательно сменить в проде** |
| `WORKSPACE_DIR`        | Песочница ExecutorAgent (sandbox)                       | опционально (`.`) |
| `LLM_TIMEOUT_SECONDS`  | Таймаут HTTP-запросов к LLM                             | опционально (`120`) |
| `MAX_ITERATIONS`       | Макс. итераций Planner↔Critic                           | опционально (`5`) |
| `PLANNER_BASE_URL`     | Базовый URL NVIDIA NIM для Planner                      | опционально |
| `CRITIC_BASE_URL`      | Базовый URL NVIDIA NIM для Critic                       | опционально |
| `PLANNER_MODEL`        | Имя модели Planner                                      | опционально |
| `CRITIC_MODEL`         | Имя модели Critic                                       | опционально |
| `EXECUTOR_MODEL`       | Имя модели Executor (Ollama)                            | опционально |
| `PLANNER_API_KEY`      | Ключ NVIDIA для Planner *(только для локальной разработки)* | в проде — через форму |
| `CRITIC_API_KEY`       | Ключ NVIDIA для Critic *(только для локальной разработки)*  | в проде — через форму |
| `OLLAMA_URL`           | Адрес локального Ollama                                 | опционально (`http://localhost:11434`) |

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
