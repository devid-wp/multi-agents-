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
    """Без cookie — дефолтные значения."""
    r = await app_client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["planner"] is None
    assert body["critic"] is None
    assert body["executor"] is None


async def test_post_settings_success_sets_cookie(app_client: httpx.AsyncClient) -> None:
    """POST с валидным payload → 200, в Set-Cookie появляется trinity_session."""
    payload = {
        "planner": {
            "provider": "nvidia",
            "api_key": "nvapi-TESTPLANNER1234567890abcdef",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "model_name": "abacusai/dracarys-llama-3.1-70b-instruct"
        },
        "critic": {
            "provider": "nvidia",
            "api_key": "nvapi-TESTCRITIC1234567890abcdef",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "model_name": "google/gemma-2-27b-it"
        },
        "executor": {
            "provider": "ollama",
            "base_url": "http://localhost:11434",
            "model_name": "qwen2.5-coder"
        }
    }
    r = await app_client.post("/api/settings", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["planner"]["has_key"] is True
    assert body["critic"]["has_key"] is True
    assert body["executor"]["has_key"] is False # Ollama has no key

    # Set-Cookie
    set_cookie = r.headers.get("set-cookie", "")
    assert "trinity_session=" in set_cookie, f"нет trinity_session в Set-Cookie: {set_cookie!r}"


async def test_post_settings_invalid_url_returns_400(app_client: httpx.AsyncClient) -> None:
    """URL без http:// → 400."""
    r = await app_client.post(
        "/api/settings",
        json={"planner": {"provider": "nvidia", "base_url": "not-a-url"}},
    )
    assert r.status_code == 400
    assert "Invalid URL" in r.json()["detail"]


async def test_post_settings_empty_key_keeps_existing(app_client: httpx.AsyncClient) -> None:
    """
    Идемпотентность: если в POST прислать пустой api_key,
    но в сессии ключ уже есть — он сохраняется.
    """
    # 1) Задаём ключ
    r1 = await app_client.post(
        "/api/settings",
        json={"planner": {"provider": "nvidia", "api_key": "nvapi-INITIAL-1234567890abcdef"}},
    )
    assert r1.status_code == 200
    cookie_jar = r1.cookies

    # 2) Шлём пустой ключ — должны оставить прежний
    r2 = await app_client.post(
        "/api/settings",
        json={"planner": {"provider": "nvidia", "api_key": ""}},
        cookies=cookie_jar,
    )
    assert r2.status_code == 200
    body = r2.json()
    
    # Проверяем через GET, что ключ остался
    r3 = await app_client.get("/api/settings", cookies=cookie_jar)
    assert r3.status_code == 200
    assert r3.json()["planner"]["has_key"] is True


async def test_post_then_get_round_trip(app_client: httpx.AsyncClient) -> None:
    """POST → GET с полученной cookie: ключ виден в маскированном виде."""
    real_key = "nvapi-ROUNDTRIP-1234567890abcdef"
    r1 = await app_client.post(
        "/api/settings",
        json={
            "planner": {
                "provider": "nvidia",
                "api_key": real_key,
                "model_name": "meta/llama-3.1-8b-instruct",
            }
        },
    )
    assert r1.status_code == 200
    cookie_jar = r1.cookies

    r2 = await app_client.get("/api/settings", cookies=cookie_jar)
    assert r2.status_code == 200
    body = r2.json()
    assert body["planner"]["has_key"] is True
    assert body["planner"]["key_masked"] == "nvapi-***cdef"
    assert body["planner"]["model_name"] == "meta/llama-3.1-8b-instruct"


async def test_post_settings_partial_update(app_client: httpx.AsyncClient) -> None:
    """POST только с одним полем (model_name) не должен затирать остальное."""
    r1 = await app_client.post(
        "/api/settings",
        json={
            "planner": {"provider": "nvidia", "api_key": "nvapi-PARTIAL-1234567890abcdef"},
            "critic": {"provider": "nvidia", "api_key": "nvapi-PARTIAL-CRITIC-1234567890ab"},
        },
    )
    assert r1.status_code == 200
    cookies = r1.cookies

    # Только меняем model_name
    r2 = await app_client.post(
        "/api/settings",
        json={"planner": {"provider": "nvidia", "model_name": "openai/gpt-oss-120b"}},
        cookies=cookies,
    )
    assert r2.status_code == 200
    cookies2 = r2.cookies

    # Проверяем, что оба ключа на месте
    r3 = await app_client.get("/api/settings", cookies=cookies2)
    body = r3.json()
    assert body["planner"]["has_key"] is True
    assert body["critic"]["has_key"] is True
    assert body["planner"]["model_name"] == "openai/gpt-oss-120b"

