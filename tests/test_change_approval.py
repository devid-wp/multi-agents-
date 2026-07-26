from pathlib import Path

import httpx

from core.models import ToolCall
from tools.registry import ToolRegistry


async def test_write_is_previewed_before_apply(
    app_client: httpx.AsyncClient, temp_workspace: Path
) -> None:
    from core.config import settings
    settings.workspace_dir = str(temp_workspace)
    registry = ToolRegistry(workspace=str(temp_workspace))
    result = await registry.execute(
        ToolCall(name="write_file", arguments={"path": "safe.txt", "content": "hello\n"})
    )
    assert result.success is True
    assert not (temp_workspace / "safe.txt").exists()

    changes = (await app_client.get("/api/changes")).json()["changes"]
    proposal = next(item for item in changes if item["path"] == "safe.txt")
    assert "+hello" in proposal["diff"]
    response = await app_client.post(
        f"/api/changes/{proposal['id']}/decision", json={"approve": True}
    )
    assert response.status_code == 200
    assert (temp_workspace / "safe.txt").read_text(encoding="utf-8") == "hello\n"


async def test_apply_rejects_stale_preview(
    app_client: httpx.AsyncClient, temp_workspace: Path
) -> None:
    from core.config import settings
    settings.workspace_dir = str(temp_workspace)
    target = temp_workspace / "race.txt"
    target.write_text("old", encoding="utf-8")
    registry = ToolRegistry(workspace=str(temp_workspace))
    await registry.execute(
        ToolCall(name="write_file", arguments={"path": "race.txt", "content": "new"})
    )
    proposal = next(
        item for item in (await app_client.get("/api/changes")).json()["changes"]
        if item["path"] == "race.txt" and item["status"] == "pending"
    )
    target.write_text("changed elsewhere", encoding="utf-8")
    response = await app_client.post(
        f"/api/changes/{proposal['id']}/decision", json={"approve": True}
    )
    assert response.status_code == 409
    assert target.read_text(encoding="utf-8") == "changed elsewhere"
