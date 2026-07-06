"""
core/config.py
────────────────
Централизованная конфигурация системы.

ВАЖНО: API-ключи НЕ хардкодятся. Они приходят из формы на сайте
и хранятся в подписанной серверной сессии (itsdangerous).
Чтение из .env — опциональный fallback для локальной разработки.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ───────────────────────────────────────────────────────────────────
# Дефолты моделей (можно переопределить через форму или .env)
# ───────────────────────────────────────────────────────────────────
DEFAULT_PLANNER_MODEL = "abacusai/dracarys-llama-3.1-70b-instruct"
DEFAULT_CRITIC_MODEL = "google/gemma-2-27b-it"
DEFAULT_EXECUTOR_MODEL = "qwen2.5-coder"

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_NVIDIA_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_OPENAI_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_GEMINI_MODEL = "gemini-1.5"


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
from core.models import AgentProviderConfig

class UserCredentials(BaseModel):
    """
    Ключи и URL, которые пользователь ввёл через форму.
    Хранятся в подписанной cookie-сессии. Никогда не логируются.
    """
    planner: Optional[AgentProviderConfig] = None
    critic: Optional[AgentProviderConfig] = None
    executor: Optional[AgentProviderConfig] = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
            
        # Legacy flat keys -> nested
        if "planner" not in data and "planner_api_key" in data:
            data["planner"] = {
                "provider": "nvidia",
                "api_key": data.get("planner_api_key"),
                "base_url": data.get("planner_base_url") or DEFAULT_NVIDIA_URL,
                "model_name": data.get("planner_model") or DEFAULT_PLANNER_MODEL
            }
        if "critic" not in data and "critic_api_key" in data:
            data["critic"] = {
                "provider": "nvidia",
                "api_key": data.get("critic_api_key"),
                "base_url": data.get("critic_base_url") or DEFAULT_NVIDIA_URL,
                "model_name": data.get("critic_model") or DEFAULT_CRITIC_MODEL
            }
        if "executor" not in data and "ollama_url" in data:
            data["executor"] = {
                "provider": "ollama",
                "api_key": None,
                "base_url": data.get("ollama_url") or DEFAULT_OLLAMA_URL,
                "model_name": data.get("executor_model") or DEFAULT_EXECUTOR_MODEL
            }
        
        # very old legacy
        legacy = data.get("nvidia_api_key")
        if legacy and str(legacy).strip():
            if "planner" not in data:
                data["planner"] = {"provider": "nvidia", "api_key": legacy, "base_url": DEFAULT_NVIDIA_URL, "model_name": DEFAULT_PLANNER_MODEL}
            if "critic" not in data:
                data["critic"] = {"provider": "nvidia", "api_key": legacy, "base_url": DEFAULT_NVIDIA_URL, "model_name": DEFAULT_CRITIC_MODEL}

        return data
