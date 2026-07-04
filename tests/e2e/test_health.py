"""
tests/e2e/test_health.py
────────────────────────
Самые простые эндпоинты: healthcheck и редирект с корня на /ui/.
"""

from __future__ import annotations

import httpx


async def test_health_returns_ok(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/api/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"ok": True, "service": "trinity"}


async def test_root_redirects_to_ui(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/", follow_redirects=False)
    assert r.status_code == 307, f"ожидался 307, получили {r.status_code}"
    # FastAPI RedirectResponse кладёт целевой URL в Location
    assert r.headers["location"].endswith("/ui/")


async def test_health_does_not_require_auth(app_client: httpx.AsyncClient) -> None:
    """Healthcheck не должен ни требовать сессию, ни трогать её."""
    r = await app_client.get("/api/health")
    assert r.status_code == 200
    # set-cookie не должно появиться
    assert "set-cookie" not in {k.lower() for k in r.headers.keys()}
