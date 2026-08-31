import httpx
from pathlib import Path


async def test_sqlite_migration_and_rooms(monkeypatch, tmp_path, app_client: httpx.AsyncClient):
    # enable sqlite for this test via settings
    from core.config import settings
    from core.db import db_path, init_db
    # ensure clean tmp is used (conftest already sets workspace_dir=tmp_path)
    settings.use_sqlite = True
    p = db_path(str(tmp_path))
    if p.exists():
        p.unlink()
    init_db(str(tmp_path))
    assert p.exists()
    # create room via API should go to sqlite
    r = await app_client.post("/api/rooms", json={"id": "sqlite-test", "name": "SQLite", "strategy": "auto"})
    assert r.status_code == 201
    # verify db contains it
    import sqlite3, json
    con = sqlite3.connect(str(p))
    cur = con.execute("SELECT data FROM rooms WHERE id=?", ("sqlite-test",))
    row = cur.fetchone()
    con.close()
    assert row is not None
    assert json.loads(row[0])["name"] == "SQLite"
    settings.use_sqlite = False
