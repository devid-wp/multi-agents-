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
# Trinity перешла на OpenRouter (облачный OpenAI-compatible провайдер).
# Разделение по ролям:
#   • Planner / Critic — крупная логика и удержание контекста
#   • Executor / остальные задачи — быстрый кодер
# ───────────────────────────────────────────────────────────────────
DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1"
DEFAULT_PLANNER_MODEL = "meta-llama/llama-3-70b-instruct:free"
DEFAULT_CRITIC_MODEL = "meta-llama/llama-3-70b-instruct:free"
DEFAULT_EXECUTOR_MODEL = "google/gemma-2-9b-it:free"

# Fallback для legacy-кода (если где-то используется напрямую)
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_NVIDIA_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_OPENAI_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_GEMINI_MODEL = "gemini-1.5"

# Back-compat alias: код, который всё ещё импортирует OLD_NVIDIA_DEFAULT_MODEL
# (например, агенты), получает тот же OpenRouter-stек, чтобы не падать
# при смене провайдера по умолчанию.
OLD_NVIDIA_DEFAULT_MODEL = DEFAULT_PLANNER_MODEL


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

    # OpenRouter — опциональный fallback-ключ из .env.
    # В проде ключ вводится через форму и хранится в подписанной сессии,
    # но для headless-сценариев (CI, скрипты) удобно задать его здесь.
    openrouter_api_key: Optional[str] = Field(
        default=None,
        description="OpenRouter API key (fallback if user didn't set it via form)",
    )
    openrouter_base_url: Optional[str] = Field(
        default=None,
        description="OpenRouter base URL override (rarely needed)",
    )

    # История диалогов — лимит сообщений до sliding window
    history_max_messages: int = Field(default=40, ge=10, le=200)

    # Workspace лимиты (вынесены из main.py hardcode)
    workspace_max_depth: int = Field(default=4, ge=1, le=10)
    workspace_max_entries: int = Field(default=1000, ge=100, le=5000)
    diagnostics_history_max: int = Field(default=500, ge=50, le=2000)

    # Circuit breaker — теперь per-provider (см. core/llm_clients.py)
    llm_circuit_breaker_threshold: int = Field(default=15, ge=5, le=100)

    # Local token (опциональный Bearer для hardening поверх localhost_only)
    # Если задан — клиенты должны слать Authorization: Bearer <token> или X-Trinity-Token
    local_token: Optional[str] = Field(default=None, description="Optional local Bearer token")

    # SQLite backend: с 0.6.0 только SQLite, JSON удалён. Флаг оставлен для совместимости (deprecated).
    use_sqlite: bool = Field(default=True, description="Deprecated: always True since 0.6.0")

    # Rate-limit для /api/chat (Фаза 3)
    chat_rate_limit_per_minute: int = Field(default=20, ge=5, le=100)


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
