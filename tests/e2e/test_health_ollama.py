import httpx
import respx


async def test_health_reports_ready_ollama(app_client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://localhost:11434/api/tags").respond(
            200, json={"models": [{"name": "qwen2.5-coder:1.5b"}]}
        )
        response = await app_client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["ollama"]["available"] is True
    assert response.json()["ollama"]["model_installed"] is True


async def test_health_stays_up_when_ollama_is_down(app_client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://localhost:11434/api/tags").mock(
            side_effect=httpx.ConnectError("offline")
        )
        response = await app_client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["ollama"]["available"] is False
