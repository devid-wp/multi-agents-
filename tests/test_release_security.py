from pathlib import Path

import httpx

from tools.registry import ToolRegistry


def test_dangerous_tools_are_not_exposed(temp_workspace: Path) -> None:
    names = set(ToolRegistry(workspace=str(temp_workspace)).list_names())
    assert {"execute_bash", "execute_git", "delete_file"}.isdisjoint(names)
    assert {"read_file", "write_file", "replace_in_file", "search_in_file", "list_dir"} <= names


async def test_non_local_client_is_rejected() -> None:
    from main import app

    transport = httpx.ASGITransport(app=app, client=("203.0.113.5", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://trinity") as client:
        response = await client.get("/api/health")
    assert response.status_code == 403


def test_release_secret_validation() -> None:
    from core.config import settings

    assert len(settings.session_secret) >= 32
