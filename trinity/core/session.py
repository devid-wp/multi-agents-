"""
core/session.py
───────────────
Безопасное хранение пользовательских кредентиалов в подписанной cookie.

Используется itsdangerous (зависимость FastAPI) — сессионные данные
хранятся на стороне клиента в зашифрованном/подписанном виде,
что позволяет не тащить Redis/БД для прототипа.
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import Request
from itsdangerous import BadSignature, URLSafeSerializer

from core.config import UserCredentials, settings

# Ключ сессии в cookie и имя «бакета» для наших данных
_SESSION_COOKIE = "trinity_session"
_CREDS_BUCKET = "creds"
_serializer = URLSafeSerializer(settings.session_secret, salt="trinity-v1")


def _read_basket(request: Request) -> dict:
    """Читает подписанный JSON-«баскет» из cookie. Пустой dict если нет."""
    raw = request.cookies.get(_SESSION_COOKIE)
    if not raw:
        return {}
    try:
        data = _serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        # Подпись не сошлась — считаем сессию пустой
        return {}


def _write_basket(basket: dict) -> str:
    """Сериализует баскет в подписанную строку для cookie."""
    return _serializer.dumps(basket)


# ───────────────────────────────────────────────────────────────────
# Публичный API
# ───────────────────────────────────────────────────────────────────
def get_credentials(request: Request) -> UserCredentials:
    """Возвращает кредентиалы пользователя из сессии (или дефолтные)."""
    basket = _read_basket(request)
    creds_dict = basket.get(_CREDS_BUCKET) or {}
    try:
        return UserCredentials(**creds_dict)
    except Exception:
        # Если данные в cookie испорчены — возвращаем дефолт
        return UserCredentials()


def save_credentials(creds: UserCredentials) -> str:
    """Возвращает подписанную строку для установки в Set-Cookie."""
    basket = {_CREDS_BUCKET: creds.model_dump(exclude_none=True)}
    return _write_basket(basket)


def mask_key(key: Optional[str]) -> Optional[str]:
    """
    Маскирует API-ключ для безопасного отображения в UI.
    'nvapi-abc123def456...' → 'nvapi-***def456'
    """
    if not key:
        return None
    if len(key) <= 8:
        return "***"
    return f"{key[:6]}***{key[-4:]}"
