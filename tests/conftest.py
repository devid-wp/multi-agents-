"""
tests/conftest.py
─────────────────
Общие фикстуры для backend-E2E тестов Trinity.

Философия:
  • `env_sandbox` (autouse) — изолируем прогон от продового `.env`:
    подменяем SESSION_SECRET, WORKSPACE_DIR на временные значения
    ДО импорта core.config (pydantic-settings читает .env при первом
    импорте, поэтому нужно аккуратно с порядком фикстур).
  • `app_client` — httpx.AsyncClient поверх ASGITransport, без сети.
  • `temp_workspace` — временная папка для workspace_dir.
  • `nvidia_keys` / `ollama_url` — optional: skip-маркеры для тестов,
    которым нужны реальные API.
"""

from __future__ import annotations

import os
import sys
import socket
import threading
import time
from pathlib import Path
from typing import AsyncGenerator, Generator, Optional

# Add the project root to sys.path to allow importing main and core
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

# ───────────────────────────────────────────────────────────────────
# Важно: env-переменные выставляем ДО импорта core.config,
# потому что pydantic-settings.AppSettings() при импорте модуля
# core.config читает .env. Поскольку pydantic-settings v2 ленив
# (читает при первом обращении к settings), но `settings = AppSettings()`
# на уровне модуля — вызывается немедленно. Поэтому фикстура
# env_sandbox должна быть autouse=True и сработать до того, как
# какой-либо тест импортирует core.config транзитивно.
# ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def env_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Изолирует тест от продового .env и домашней папки.
    Задаёт SESSION_SECRET (тестовый) и WORKSPACE_DIR (= tmp_path).
    """
    monkeypatch.setenv("SESSION_SECRET", "test-secret-do-not-use-in-prod-0123456789abcdef")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    # Очищаем продовые ключи, если они случайно есть в env —
    # иначе тесты будут «протекать» в реальный NVIDIA.
    monkeypatch.delenv("PLANNER_API_KEY", raising=False)
    monkeypatch.delenv("CRITIC_API_KEY", raising=False)
    # OLLAMA_URL оставляем как есть — это не секрет, и тесты Ollama
    # могут его использовать.


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Path:
    """
    Временная директория для workspace.

    ВАЖНО: env_sandbox (autouse) ставит WORKSPACE_DIR=str(tmp_path) для
    ЭТОГО ЖЕ `tmp_path`. pytest-фикстура `tmp_path` создаётся ОДИН раз
    на тест — оба получают один и тот же объект, поэтому директория
    эндпоинта /api/workspace/tree — это и есть наш temp_workspace.
    """
    return tmp_path


# ───────────────────────────────────────────────────────────────────
# ASGI-клиент — гоняем FastAPI-приложение in-process.
# ───────────────────────────────────────────────────────────────────
@pytest.fixture
async def app_client() -> AsyncGenerator["httpx.AsyncClient", None]:
    """
    httpx.AsyncClient поверх ASGITransport.
    Не открывает сетевой сокет — запросы идут прямо в FastAPI.
    """
    import httpx

    # Импортируем main лениво, чтобы фикстура env_sandbox успела
    # выставиться раньше, чем AppSettings() прочитает .env.
    from main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        yield client


# ───────────────────────────────────────────────────────────────────
# Live server — uvicorn в фоновом потоке для SSE-тестов.
# httpx.ASGITransport дедлочится на бесконечных SSE-стримах, поэтому
# SSE E2E-тесты используют реальный HTTP через localhost.
# ───────────────────────────────────────────────────────────────────
@pytest.fixture
def live_server_url() -> Generator[str, None, None]:
    """
    Запускает uvicorn с FastAPI-приложением на случайном свободном порту
    в daemon-потоке. Ждёт готовности сервера (TCP connect).
    Yields: строку вида 'http://127.0.0.1:<port>'.
    """
    import uvicorn
    from main import app

    # Найдём свободный порт
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Ждём, пока порт откроется (до 5 секунд)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        raise RuntimeError(f"uvicorn не запустился на порту {port}")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=3.0)


# ───────────────────────────────────────────────────────────────────
# Optional: real-API ключи.
# Если переменных окружения нет — соответствующие тесты skip'аются.
# ───────────────────────────────────────────────────────────────────
@pytest.fixture
def nvidia_keys() -> dict:
    """
    Возвращает {planner_api_key, critic_api_key, planner_base_url, critic_base_url}
    из env. Если ключей нет — pytest.skip с понятным сообщением.
    """
    pk = os.environ.get("PLANNER_API_KEY", "").strip()
    ck = os.environ.get("CRITIC_API_KEY", "").strip()
    if not pk or not ck:
        pytest.skip(
            "PLANNER_API_KEY / CRITIC_API_KEY не заданы — "
            "real-API тест пропущен. Установите в env, чтобы запустить."
        )
    return {
        "planner_api_key": pk,
        "critic_api_key": ck,
        "planner_base_url": os.environ.get("PLANNER_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        "critic_base_url": os.environ.get("CRITIC_BASE_URL", "https://integrate.api.nvidia.com/v1"),
    }


@pytest.fixture
def ollama_url() -> str:
    """Возвращает OLLAMA_URL, либо skip, если не задан."""
    url = os.environ.get("OLLAMA_URL", "").strip()
    if not url:
        pytest.skip("OLLAMA_URL не задан — Ollama real-API тест пропущен.")
    return url
