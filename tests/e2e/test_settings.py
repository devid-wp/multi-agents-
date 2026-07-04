"""
tests/e2e/test_settings.py
──────────────────────────
GET / POST /api/settings + cookie-сессия.

Покрывает:
  • дефолтное состояние (без cookie)
  • успешный POST с валидным payload → Set-Cookie + body
  • невалидный URL → 400
  • идемпотентность: пустой ключ при существующем = оставить
  • round-trip: POST → GET с полученной cookie видит ключ
  • маскирование ключа в ответе
"""

from __future__ import annotations

import httpx


async def test_get_settings_defaults(app_client: httpx.AsyncClient) -> None:
    """Без cookie — дефолтные значения, оба ключа пустые."""
    r = await app_client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["has_planner_key"] is False
    assert body["has_critic_key"] is False
    # Дефолтный URL
    assert body["planner_base_url"].startswith("https://")
    assert body["ollama_url"].startswith("http://")
    # Маски ключей пустые
    assert body["planner_key_masked"] in (None, "")
    assert body["critic_key_masked"] in (None, "")


async def test_post_settings_success_sets_cookie(app_client: httpx.AsyncClient) -> None:
    """POST с валидным payload → 200, в Set-Cookie появляется trinity_session."""
    payload = {
        "planner_api_key": "nvapi-TESTPLANNER1234567890abcdef",
        "planner_base_url": "https://integrate.api.nvidia.com/v1",
        "critic_api_key": "nvapi-TESTCRITIC1234567890abcdef",
        "critic_base_url": "https://integrate.api.nvidia.com/v1",
        "ollama_url": "http://localhost:11434",
        "planner_model": "abacusai/dracarys-llama-3.1-70b-instruct",
        "critic_model": "google/gemma-2-27b-it",
        "executor_model": "qwen2.5-coder",
    }
    r = await app_client.post("/api/settings", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["has_planner_key"] is True
    assert body["has_critic_key"] is True

    # Set-Cookie
    set_cookie = r.headers.get("set-cookie", "")
    assert "trinity_session=" in set_cookie, f"нет trinity_session в Set-Cookie: {set_cookie!r}"
    # HttpOnly
    assert "HttpOnly" in set_cookie or "httponly" in set_cookie.lower()
    # SameSite
    assert "samesite=lax" in set_cookie.lower()


async def test_post_settings_invalid_url_returns_400(app_client: httpx.AsyncClient) -> None:
    """URL без http:// → 400."""
    r = await app_client.post(
        "/api/settings",
        json={"planner_base_url": "not-a-url"},
    )
    assert r.status_code == 400
    assert "Invalid URL" in r.json()["detail"]


async def test_post_settings_empty_key_keeps_existing(app_client: httpx.AsyncClient) -> None:
    """
    Идемпотентность: если в POST прислать пустой planner_api_key,
    но в сессии ключ уже есть — он сохраняется.
    """
    # 1) Задаём ключ
    r1 = await app_client.post(
        "/api/settings",
        json={"planner_api_key": "nvapi-INITIAL-1234567890abcdef"},
    )
    assert r1.status_code == 200
    cookie_jar = r1.cookies  # httpx сам подхватывает Set-Cookie

    # 2) Шлём пустой ключ — должны оставить прежний
    r2 = await app_client.post(
        "/api/settings",
        json={"planner_api_key": ""},  # пустая строка
        cookies=cookie_jar,
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["ok"] is True
    # Проверяем через GET, что ключ остался
    r3 = await app_client.get("/api/settings", cookies=cookie_jar)
    assert r3.status_code == 200
    assert r3.json()["has_planner_key"] is True


async def test_post_then_get_round_trip(app_client: httpx.AsyncClient) -> None:
    """POST → GET с полученной cookie: ключ виден в маскированном виде."""
    real_key = "nvapi-ROUNDTRIP-1234567890abcdef"
    r1 = await app_client.post(
        "/api/settings",
        json={
            "planner_api_key": real_key,
            "planner_model": "meta/llama-3.1-8b-instruct",
        },
    )
    assert r1.status_code == 200
    cookie_jar = r1.cookies

    r2 = await app_client.get("/api/settings", cookies=cookie_jar)
    assert r2.status_code == 200
    body = r2.json()
    assert body["has_planner_key"] is True
    # Маска: первые 6 + "***" + последние 4 (см. core/session.mask_key)
    # "nvapi-ROUNDTRIP-1234567890abcdef" -> "nvapi-" + "***" + "cdef"
    assert body["planner_key_masked"] == "nvapi-***cdef"
    assert body["planner_model"] == "meta/llama-3.1-8b-instruct"


async def test_post_settings_partial_update(app_client: httpx.AsyncClient) -> None:
    """POST только с одним полем (planner_model) не должен затирать остальное."""
    r1 = await app_client.post(
        "/api/settings",
        json={
            "planner_api_key": "nvapi-PARTIAL-1234567890abcdef",
            "critic_api_key": "nvapi-PARTIAL-CRITIC-1234567890ab",
        },
    )
    assert r1.status_code == 200
    cookies = r1.cookies

    # Только меняем planner_model
    r2 = await app_client.post(
        "/api/settings",
        json={"planner_model": "openai/gpt-oss-120b"},
        cookies=cookies,
    )
    assert r2.status_code == 200

    # Проверяем, что оба ключа на месте
    r3 = await app_client.get("/api/settings", cookies=cookies)
    body = r3.json()
    assert body["has_planner_key"] is True
    assert body["has_critic_key"] is True
    assert body["planner_model"] == "openai/gpt-oss-120b"


async def test_post_settings_legacy_single_key_migrates(app_client: httpx.AsyncClient) -> None:
    """
    Legacy-миграция: если прислали nvidia_api_key (старый формат),
    он подхватывается в оба (planner + critic).
    """
    r = await app_client.post(
        "/api/settings",
        json={"nvidia_api_key": "nvapi-LEGACY-1234567890abcdef"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["has_planner_key"] is True
    assert body["has_critic_key"] is True
