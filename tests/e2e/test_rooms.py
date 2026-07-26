from uuid import uuid4

import httpx


async def test_general_room_always_exists(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/api/rooms")
    assert response.status_code == 200
    assert response.json()["rooms"][0]["id"] == "general"


async def test_create_room_persists(app_client: httpx.AsyncClient) -> None:
    room_id = f"backend-{uuid4().hex[:8]}"
    response = await app_client.post(
        "/api/rooms", json={"id": room_id, "name": "Backend", "strategy": "planner"}
    )
    assert response.status_code == 201
    rooms = (await app_client.get("/api/rooms")).json()["rooms"]
    assert any(room["id"] == room_id for room in rooms)


async def test_history_is_scoped_by_room(app_client: httpx.AsyncClient) -> None:
    missing = await app_client.get(
        "/api/chat/history", params={"session_id": "browser", "room_id": "missing"}
    )
    assert missing.status_code == 404
