"""
core/config.py
────────────────
Централизованная конфигурация системы.

ВАЖНО: API-ключи НЕ хардкодятся. Они приходят из формы на сайте
и хранятся в подписанной серверной сессии (itsdangerous).
Чтение из .env — опциональный fallback для локальной разработки.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ───────────────────────────────────────────────────────────────────
# Дефолты моделей (можно переопределить через форму или .env)
# ───────────────────────────────────────────────────────────────────
DEFAULT_PLANNER_MODEL = "abacusai/dracarys-llama-3.1-70b-instruct"
DEFAULT_CRITIC_MODEL = "google/gemma-2-27b-it"
DEFAULT_EXECUTOR_MODEL = "qwen2.5-coder"

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_NVIDIA_URL = "https://integrate.api.nvidia.com/v1"


class AppSettings(BaseSettings):
    """
    Серверные настройки приложения.
    Берутся из .env (если есть) или переменных окружения.
    Чувствительные данные (API-ключи) сюда НЕ попадают —
    они живут в сессии пользователя.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Секрет для подписи сессионных cookie (генерируется при первом запуске,
    # если не задан в .env)
    session_secret: str = Field(
        default="change-me-in-production-please-use-strong-secret",
        description="Itsdangerous secret for session signing",
    )

    # Путь к директории, в которой ExecutorAgent может читать/писать файлы.
    # По умолчанию — текущая рабочая директория, где запущен uvicorn.
    workspace_dir: str = Field(
        default=".",
        description="Sandbox directory for file tools",
    )

    # Таймаут HTTP-запросов к LLM-провайдерам
    llm_timeout_seconds: int = Field(default=120, ge=10, le=600)

    # Максимальное количество итераций в цикле Planner→Critic→Executor
    max_iterations: int = Field(default=5, ge=1, le=20)


# Глобальный singleton — инициализируется один раз при импорте
settings = AppSettings()


# ───────────────────────────────────────────────────────────────────
# Сессионные настройки (вводятся пользователем через форму)
# ───────────────────────────────────────────────────────────────────
class UserCredentials(BaseModel):
    """
    Ключи и URL, которые пользователь ввёл через форму.
    Хранятся в подписанной cookie-сессии. Никогда не логируются.
    """

    nvidia_api_key: Optional[str] = Field(
        default=None,
        description="NVIDIA NIM API key (build.nvidia.com)",
    )
    ollama_url: str = Field(
        default=DEFAULT_OLLAMA_URL,
        description="Base URL for local Ollama server",
    )

    # Можно переопределить модели (для экспериментов)
    planner_model: str = Field(default=DEFAULT_PLANNER_MODEL)
    critic_model: str = Field(default=DEFAULT_CRITIC_MODEL)
    executor_model: str = Field(default=DEFAULT_EXECUTOR_MODEL)

    def has_nvidia(self) -> bool:
        """True, если ключ NVIDIA введён и непустой."""
        return bool(self.nvidia_api_key and self.nvidia_api_key.strip())

    def has_ollama(self) -> bool:
        """True, если Ollama URL валиден."""
        return bool(self.ollama_url and self.ollama_url.strip())
