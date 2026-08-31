from uuid import uuid4
import httpx


async def test_rename_room(app_client: httpx.AsyncClient) -> None:
    room_id = f"rename-{uuid4().hex[:6]}"
    r = await app_client.post("/api/rooms", json={"id": room_id, "name": "Orig", "strategy": "auto"})
    assert r.status_code == 201
    upd = await app_client.put(f"/api/rooms/{room_id}", json={"name": "Renamed"})
    assert upd.status_code == 200
    assert upd.json()["name"] == "Renamed"
    rooms = (await app_client.get("/api/rooms")).json()["rooms"]
    assert any(x["id"] == room_id and x["name"] == "Renamed" for x in rooms)


async def test_delete_room(app_client: httpx.AsyncClient) -> None:
    room_id = f"del-{uuid4().hex[:6]}"
    r = await app_client.post("/api/rooms", json={"id": room_id, "name": "ToDel", "strategy": "auto"})
    assert r.status_code == 201
    d = await app_client.delete(f"/api/rooms/{room_id}")
    assert d.status_code == 200
    rooms = (await app_client.get("/api/rooms")).json()["rooms"]
    assert not any(x["id"] == room_id for x in rooms)


async def test_cannot_rename_or_delete_general(app_client: httpx.AsyncClient) -> None:
    r1 = await app_client.put("/api/rooms/general", json={"name": "New"})
    assert r1.status_code == 400
    r2 = await app_client.delete("/api/rooms/general")
    assert r2.status_code == 400
