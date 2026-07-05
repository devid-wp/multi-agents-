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
class UserCredentials(BaseModel):
    """
    Ключи и URL, которые пользователь ввёл через форму.
    Хранятся в подписанной cookie-сессии. Никогда не логируются.

    NVIDIA-провайдеров теперь ДВА — отдельный ключ и base URL
    для Planner и Critic (на NIM эндпоинты и квоты часто разные).
    """

    # ── NVIDIA: Planner ────────────────────────────────────────────
    planner_api_key: Optional[str] = Field(
        default=None,
        description="NVIDIA NIM API key for Planner agent",
    )
    planner_base_url: str = Field(
        default=DEFAULT_NVIDIA_URL,
        description="Base URL for Planner (NVIDIA NIM)",
    )
    # Полный URL эндпоинта модели (опционально). Если задан — перекрывает
    # {base_url}/chat/completions. Полезно, когда у Critic/Planner разные
    # NIM-деплойменты с собственными путями.
    planner_model_url: Optional[str] = Field(
        default=None,
        description="Full model URL for Planner (overrides base_url + /chat/completions)",
    )

    # ── NVIDIA: Critic ─────────────────────────────────────────────
    critic_api_key: Optional[str] = Field(
        default=None,
        description="NVIDIA NIM API key for Critic agent",
    )
    critic_base_url: str = Field(
        default=DEFAULT_NVIDIA_URL,
        description="Base URL for Critic (NVIDIA NIM)",
    )
    critic_model_url: Optional[str] = Field(
        default=None,
        description="Full model URL for Critic (overrides base_url + /chat/completions)",
    )

    # ── Ollama (Executor) ──────────────────────────────────────────
    ollama_url: str = Field(
        default=DEFAULT_OLLAMA_URL,
        description="Base URL for local Ollama server",
    )

    # ── OpenAI / Gemini
    openai_api_key: Optional[str] = Field(
        default=None,
        description="OpenAI API key for direct-response provider",
    )
    openai_base_url: Optional[str] = Field(
        default=DEFAULT_OPENAI_URL,
        description="Custom OpenAI-compatible base URL",
    )
    openai_model: Optional[str] = Field(
        default=DEFAULT_OPENAI_MODEL,
        description="OpenAI model name to use for direct response",
    )
    gemini_api_key: Optional[str] = Field(
        default=None,
        description="Gemini API key for direct-response provider",
    )
    gemini_base_url: Optional[str] = Field(
        default=None,
        description="Custom Gemini-compatible base URL",
    )
    gemini_model: Optional[str] = Field(
        default=DEFAULT_GEMINI_MODEL,
        description="Gemini model name to use for direct response",
    )

    # ── Модели ─────────────────────────────────────────────────────
    planner_model: str = Field(default=DEFAULT_PLANNER_MODEL)
    critic_model: str = Field(default=DEFAULT_CRITIC_MODEL)
    executor_model: str = Field(default=DEFAULT_EXECUTOR_MODEL)

    # ── Legacy-миграция: старые сессии с одним nvidia_api_key ─────
    # Если в cookie прилетел старый формат с nvidia_api_key
    # и новые поля пустые — копируем в оба, чтобы ничего не сломалось.
    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        legacy = data.get("nvidia_api_key")
        if not legacy:
            return data
        if not (legacy or "").strip():
            return data
        # Если новые поля не заполнены — подхватываем из legacy
        if not data.get("planner_api_key"):
            data["planner_api_key"] = legacy
        if not data.get("critic_api_key"):
            data["critic_api_key"] = legacy
        if not data.get("planner_base_url"):
            data["planner_base_url"] = DEFAULT_NVIDIA_URL
        if not data.get("critic_base_url"):
            data["critic_base_url"] = DEFAULT_NVIDIA_URL
        return data

    # ── Helpers ────────────────────────────────────────────────────
    def has_planner_key(self) -> bool:
        """True, если ключ NVIDIA для Planner введён и непустой."""
        return bool(self.planner_api_key and self.planner_api_key.strip())

    def has_critic_key(self) -> bool:
        """True, если ключ NVIDIA для Critic введён и непустой."""
        return bool(self.critic_api_key and self.critic_api_key.strip())

    def has_openai_key(self) -> bool:
        """True, если ключ OpenAI введён и непустой."""
        return bool(self.openai_api_key and self.openai_api_key.strip())

    def has_gemini_key(self) -> bool:
        """True, если ключ Gemini введён и непустой."""
        return bool(self.gemini_api_key and self.gemini_api_key.strip())

    def has_ollama(self) -> bool:
        """True, если Ollama URL валиден."""
        return bool(self.ollama_url and self.ollama_url.strip())

    def providers_dict(self) -> Dict[str, Dict[str, str]]:
        """
        Возвращает словарь провайдеров в формате, удобном для NvidiaClient:
          {AgentName.PLANNER: {"api_key": ..., "base_url": ..., "model_url": ...}, ...}
        Отсутствующие ключи просто не попадают в словарь.
        """
        from core.models import AgentName

        out: Dict[str, Dict[str, str]] = {}
        if self.has_planner_key():
            entry: Dict[str, str] = {
                "api_key": self.planner_api_key or "",
                "base_url": self.planner_base_url,
            }
            if self.planner_model_url:
                entry["model_url"] = self.planner_model_url
            out[AgentName.PLANNER.value] = entry
        if self.has_critic_key():
            entry = {
                "api_key": self.critic_api_key or "",
                "base_url": self.critic_base_url,
            }
            if self.critic_model_url:
                entry["model_url"] = self.critic_model_url
            out[AgentName.CRITIC.value] = entry
        return out
