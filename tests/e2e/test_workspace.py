"""
tests/e2e/test_workspace.py
────────────────────────────
GET /api/workspace/tree  — JSON-снимок.
GET /api/workspace/stream — SSE-стрим изменений через watchfiles.

Тонкости:
  • WORKSPACE_DIR подменяется env_sandbox-фикстурой на tmp_path, поэтому
    обход начинается с пустой временной папки. Перед тестами мы создаём
    в ней файл/папку, чтобы было что показать.
  • Дефолт `path="."` означает корень workspace.
  • Игнорируемые каталоги (.git, __pycache__, …) не попадают в entries.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest


# ───────────────────────────────────────────────────────────────────
# /api/workspace/tree
# ───────────────────────────────────────────────────────────────────
async def test_tree_root_returns_non_empty_entries(
    app_client: httpx.AsyncClient, temp_workspace: Path
) -> None:
    """GET /api/workspace/tree?path=. → 200, entries непустой, truncated — bool."""
    # Кладём в tmp_path пару файлов и папку, чтобы было что перечислять
    (temp_workspace / "hello.txt").write_text("hi", encoding="utf-8")
    sub = temp_workspace / "subdir"
    sub.mkdir()
    (sub / "inner.txt").write_text("inside", encoding="utf-8")

    r = await app_client.get("/api/workspace/tree", params={"path": "."})
    assert r.status_code == 200, r.text
    body = r.json()

    assert isinstance(body["entries"], list)
    assert body["truncated"] is False  # не превысили max_entries
    assert body["count"] == len(body["entries"])
    # Должны увидеть оба наших файла
    paths = {e["path"] for e in body["entries"]}
    assert "hello.txt" in paths
    assert "subdir" in paths
    # И подкаталог тоже обойдён (max_depth=4, depth 1)
    assert "subdir/inner.txt" in paths


async def test_tree_excludes_git_and_pycache(
    app_client: httpx.AsyncClient, temp_workspace: Path
) -> None:
    """Скрытые/служебные каталоги фильтруются и не появляются в entries."""
    (temp_workspace / "real.txt").write_text("ok", encoding="utf-8")
    (temp_workspace / ".git").mkdir()
    (temp_workspace / ".git" / "config").write_text("x", encoding="utf-8")
    (temp_workspace / "__pycache__").mkdir()
    (temp_workspace / "__pycache__" / "mod.pyc").write_text("x", encoding="utf-8")

    r = await app_client.get("/api/workspace/tree")
    assert r.status_code == 200
    body = r.json()
    names = [e["path"] for e in body["entries"]]
    # Никаких .git / __pycache__ ни на каком уровне
    assert not any(n.startswith(".git") for n in names)
    assert not any(n.startswith("__pycache__") for n in names)
    assert "real.txt" in names


async def test_tree_path_traversal_returns_400(
    app_client: httpx.AsyncClient,
) -> None:
    """GET ?path=../etc → 400 (path traversal)."""
    r = await app_client.get("/api/workspace/tree", params={"path": "../etc"})
    assert r.status_code == 400
    assert "traversal" in r.json()["detail"].lower()


async def test_tree_empty_path_equals_dot(
    app_client: httpx.AsyncClient, temp_workspace: Path
) -> None:
    """GET ?path= (пустая строка) → 200, эквивалентно '.'."""
    (temp_workspace / "a.txt").write_text("a", encoding="utf-8")
    r = await app_client.get("/api/workspace/tree", params={"path": ""})
    assert r.status_code == 200, r.text
    body = r.json()
    assert any(e["path"] == "a.txt" for e in body["entries"])


# ───────────────────────────────────────────────────────────────────
# /api/workspace/stream (SSE)
# ───────────────────────────────────────────────────────────────────
async def test_workspace_stream_emits_create_event(
    app_client: httpx.AsyncClient, temp_workspace: Path
) -> None:
    """
    Создаём файл во временной workspace и читаем 1–2 SSE-кадра из стрима.
    Ожидаем кадр data: {"type":"created","path":…} (или modified).

    Таймаут 5 с, чтобы тест не завис, если watchfiles молчит.
    Требует watchfiles — это soft-check, чтобы не падать на среде без него.
    """
    pytest.importorskip("watchfiles")

    target = temp_workspace / "new_file.txt"
    captured: list[dict] = []

    async with app_client.stream(
        "GET", "/api/workspace/stream"
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        assert resp.headers["content-type"].startswith("text/event-stream")

        # Создаём файл уже после открытия стрима — даём watcher-у шанс увидеть.
        # Небольшая задержка, чтобы awatch стартовал до изменения.
        await asyncio.sleep(0.5)
        target.write_text("payload", encoding="utf-8")

        async def _read_events(limit: int = 3, timeout: float = 5.0):
            """Читает SSE-кадры до первого `data: {…}` (или timeout)."""
            async def _iter():
                async for line in resp.aiter_lines():
                    yield line

            async with asyncio.timeout(timeout):
                async for line in _iter():
                    if line.startswith("data:"):
                        payload = line[len("data:"):].strip()
                        try:
                            ev = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        captured.append(ev)
                        if len(captured) >= limit:
                            return

        try:
            await _read_events()
        except (asyncio.TimeoutError, TimeoutError):
            pytest.fail(
                f"Не дождались SSE-события о создании файла за 5 с. "
                f"captured={captured!r}"
            )

    # Хотя бы одно событие должно ссылаться на наш файл
    paths = [ev.get("path") for ev in captured]
    assert any(p and Path(p).name == "new_file.txt" for p in paths), (
        f"Ожидался event с path=new_file.txt, получили: {captured!r}"
    )
    # Тип — один из {created, modified} (FS-наблюдатели не всегда отличают)
    types = {ev.get("type") for ev in captured}
    assert types & {"created", "modified"}, f"Не было create/modify: {types!r}"
