# Trinity — Multi-Agent System

Модульная мульти-агентная система на FastAPI, состоящая из трёх специализированных агентов:

- **PlannerAgent** — стратегическое планирование (NVIDIA: `abacusai/dracarys-llama-3.1-70b-instruct`)
- **CriticAgent** — критический анализ и ревью (NVIDIA: `google/gemma-2-27b-it`)
- **ExecutorAgent** — исполнение кода и работа с файлами (Ollama: `qwen2.5-coder`)

## Структура проекта

```
trinity/
├── main.py                  # FastAPI entry-point
├── requirements.txt
├── core/                    # Ядро системы
│   ├── __init__.py
│   ├── config.py            # Настройки (без хардкода)
│   ├── models.py            # Pydantic-модели сообщений
│   ├── session.py           # Хранение сессии
│   └── llm_clients.py       # Клиенты NVIDIA и Ollama
├── agents/                  # Реализации агентов
│   ├── __init__.py
│   ├── base.py              # Базовый класс Agent
│   ├── planner.py
│   ├── critic.py
│   ├── executor.py
│   └── manager.py           # AgentManager — координатор
├── tools/                   # Cline-like инструменты
│   ├── __init__.py
│   ├── base.py
│   ├── bash_tool.py
│   ├── file_tool.py
│   └── registry.py
├── static/                  # CSS, JS
│   ├── style.css
│   └── app.js
└── templates/               # Jinja2
    └── index.html
```

## Запуск

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Откройте `http://127.0.0.1:8000` и введите API-ключи в форме.
