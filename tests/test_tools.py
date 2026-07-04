"""
tests/test_tools.py
───────────────────
Unit-тесты Toolbox-этапа:

  • SecurityError поднимается при выходе за workspace.
  • DeleteFile удаляет файл в workspace; блокируется на ..-путях.
  • SearchInFile находит regex; ограничивает max_results; не уходит
    за пределы workspace даже на symlink-ловушках.
  • ToolRegistry.execute() эмитит 'tool_execution' в diagnostics_bus
    ДО запуска tool-а (даже если tool заблокирован sandbox-ом или
    не существует).
  • Agent.parse_json_tool_calls понимает <tool_call>...</tool_call>.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

# ───────────────────────────────────────────────────────────────────
# Подгружаем общие фикстуры через conftest (env_sandbox, temp_workspace).
# ───────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────
# SecurityError
# ───────────────────────────────────────────────────────────────────
class TestSecurityError:
    def test_is_permission_error_subclass(self) -> None:
        """SecurityError должен быть PermissionError, чтобы старые
        обработчики `except PermissionError` его ловили."""
        from tools.exceptions import SecurityError

        err = SecurityError("boom")
        assert isinstance(err, PermissionError)
        assert str(err) == "boom"

    def test_safe_resolve_blocks_parent_traversal(self, temp_workspace: Path) -> None:
        """Путь через `..` должен вызвать SecurityError."""
        from tools.exceptions import SecurityError
        from tools.file_tool import _safe_resolve

        with pytest.raises(SecurityError):
            _safe_resolve(str(temp_workspace), "../../../etc/passwd")

    def test_safe_resolve_blocks_absolute_outside(self, temp_workspace: Path) -> None:
        """Абсолютный путь за пределами workspace → SecurityError."""
        from tools.exceptions import SecurityError
        from tools.file_tool import _safe_resolve

        with pytest.raises(SecurityError):
            _safe_resolve(str(temp_workspace), "/etc/passwd")

    def test_safe_resolve_allows_inside(self, temp_workspace: Path) -> None:
        """Путь ВНУТРИ workspace — окей, возвращает Path."""
        from tools.file_tool import _safe_resolve

        target = temp_workspace / "subdir" / "file.txt"
        resolved = _safe_resolve(str(temp_workspace), "subdir/file.txt")
        assert resolved == target


# ───────────────────────────────────────────────────────────────────
# DeleteFile
# ───────────────────────────────────────────────────────────────────
class TestDeleteFile:
    @pytest.mark.asyncio
    async def test_deletes_file(self, temp_workspace: Path) -> None:
        from tools.file_tool import DeleteFile

        target = temp_workspace / "to_remove.txt"
        target.write_text("bye", encoding="utf-8")
        assert target.exists()

        tool = DeleteFile(workspace=str(temp_workspace))
        result = await tool.execute({"path": "to_remove.txt"})

        assert "[OK]" in result
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_blocks_outside_workspace(self, temp_workspace: Path) -> None:
        from tools.file_tool import DeleteFile

        tool = DeleteFile(workspace=str(temp_workspace))
        result = await tool.execute({"path": "../../../etc/passwd"})

        assert "[BLOCKED]" in result

    @pytest.mark.asyncio
    async def test_refuses_directory(self, temp_workspace: Path) -> None:
        """delete_file НЕ ДОЛЖЕН сносить директории (защита от rm -rf)."""
        from tools.file_tool import DeleteFile

        (temp_workspace / "subdir").mkdir()
        (temp_workspace / "subdir" / "a.txt").write_text("x", encoding="utf-8")

        tool = DeleteFile(workspace=str(temp_workspace))
        result = await tool.execute({"path": "subdir"})

        assert "[ERROR]" in result
        assert (temp_workspace / "subdir").exists()
        assert (temp_workspace / "subdir" / "a.txt").exists()

    @pytest.mark.asyncio
    async def test_missing_file(self, temp_workspace: Path) -> None:
        from tools.file_tool import DeleteFile

        tool = DeleteFile(workspace=str(temp_workspace))
        result = await tool.execute({"path": "nope.txt"})

        assert "[ERROR]" in result
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_missing_path_argument(self, temp_workspace: Path) -> None:
        from tools.file_tool import DeleteFile

        tool = DeleteFile(workspace=str(temp_workspace))
        result = await tool.execute({})

        assert "[ERROR]" in result


# ───────────────────────────────────────────────────────────────────
# SearchInFile
# ───────────────────────────────────────────────────────────────────
class TestSearchInFile:
    @pytest.mark.asyncio
    async def test_finds_in_single_file(self, temp_workspace: Path) -> None:
        from tools.file_tool import SearchInFile

        f = temp_workspace / "code.py"
        f.write_text(
            "def foo():\n    return 42\n\ndef bar():\n    return foo()\n",
            encoding="utf-8",
        )

        tool = SearchInFile(workspace=str(temp_workspace))
        result = await tool.execute({"path": "code.py", "pattern": r"def \w+"})

        # 2 матча: def foo, def bar
        assert "def foo" in result
        assert "def bar" in result
        assert "code.py:1:" in result or "code.py:4:" in result

    @pytest.mark.asyncio
    async def test_recursive_directory(self, temp_workspace: Path) -> None:
        from tools.file_tool import SearchInFile

        (temp_workspace / "a.py").write_text("alpha = 1\n", encoding="utf-8")
        (temp_workspace / "b.txt").write_text("beta text\n", encoding="utf-8")
        (temp_workspace / "sub").mkdir()
        (temp_workspace / "sub" / "c.py").write_text("gamma = 1\n", encoding="utf-8")

        tool = SearchInFile(workspace=str(temp_workspace))
        result = await tool.execute({"path": ".", "pattern": r"= 1"})

        # Должны найтись оба = 1
        assert "alpha" in result
        assert "gamma" in result
        # beta без = 1
        assert "beta" not in result

    @pytest.mark.asyncio
    async def test_blocks_outside_workspace(self, temp_workspace: Path) -> None:
        from tools.file_tool import SearchInFile

        tool = SearchInFile(workspace=str(temp_workspace))
        result = await tool.execute({"path": "../../../etc", "pattern": ".*"})

        assert "[BLOCKED]" in result

    @pytest.mark.asyncio
    async def test_invalid_regex(self, temp_workspace: Path) -> None:
        from tools.file_tool import SearchInFile

        f = temp_workspace / "a.txt"
        f.write_text("hello", encoding="utf-8")

        tool = SearchInFile(workspace=str(temp_workspace))
        result = await tool.execute({"path": "a.txt", "pattern": "(["})

        assert "[ERROR]" in result
        assert "regex" in result.lower()

    @pytest.mark.asyncio
    async def test_max_results_truncation(self, temp_workspace: Path) -> None:
        from tools.file_tool import SearchInFile

        f = temp_workspace / "big.txt"
        # 50 строк, каждая содержит 'X'
        f.write_text("\n".join(f"line{i} X" for i in range(50)), encoding="utf-8")

        tool = SearchInFile(workspace=str(temp_workspace))
        result = await tool.execute({
            "path": "big.txt",
            "pattern": "X",
            "max_results": 5,
        })

        # Должно быть 5 матчей + строка truncated
        assert "truncated" in result
        # Проверяем, что строк ровно 5 + truncation = 6
        lines = [ln for ln in result.splitlines() if ln]
        assert len(lines) == 6  # 5 матчей + 1 строка "...truncated..."

    @pytest.mark.asyncio
    async def test_no_matches(self, temp_workspace: Path) -> None:
        from tools.file_tool import SearchInFile

        (temp_workspace / "x.txt").write_text("hello\n", encoding="utf-8")
        tool = SearchInFile(workspace=str(temp_workspace))
        result = await tool.execute({"path": "x.txt", "pattern": "ZZZ_NEVER"})

        assert "no matches" in result.lower()


# ───────────────────────────────────────────────────────────────────
# ToolRegistry → diagnostics_bus.publish_tool_execution
# ───────────────────────────────────────────────────────────────────
class TestDiagnosticsEmission:
    @pytest.mark.asyncio
    async def test_emits_tool_execution_on_success(self, temp_workspace: Path) -> None:
        from core.diagnostics import diagnostics_bus
        from core.models import ToolCall
        from tools.registry import ToolRegistry

        # Чистим буфер шины для детерминированного подсчёта.
        # Берём срез до теста, чтобы не зависеть от соседних тестов.
        history_before = len(diagnostics_bus._buffer)

        target = temp_workspace / "h.txt"
        target.write_text("hi", encoding="utf-8")

        reg = ToolRegistry(workspace=str(temp_workspace))
        result = await reg.execute(ToolCall(name="read_file", arguments={"path": "h.txt"}))

        assert result.success
        # В буфере должна появиться запись tool_execution.
        history_after = list(diagnostics_bus._buffer)[history_before:]
        tool_events = [
            json.loads(line) for line in history_after
            if '"kind": "tool_execution"' in line
        ]
        assert any(ev.get("tool") == "read_file" for ev in tool_events), tool_events

    @pytest.mark.asyncio
    async def test_emits_on_blocked_path(self, temp_workspace: Path) -> None:
        """SecurityError НЕ ДОЛЖЕН проглатывать эмит tool_execution —
        иначе в Live Log Stream не видно, что LLM пыталась выйти за
        sandbox.

        NB: file-tool-ы (ReadFile/WriteFile/DeleteFile/SearchInFile) сами
        ловят SecurityError внутри execute() и возвращают текстовый
        маркер [BLOCKED] с success=True (это by design — LLM должна
        увидеть ошибку как обычный string, а не exception). Главное —
        что эмит tool_execution ДО этого всё равно сработал.
        """
        from core.diagnostics import diagnostics_bus
        from core.models import ToolCall
        from tools.registry import ToolRegistry

        history_before = len(diagnostics_bus._buffer)

        reg = ToolRegistry(workspace=str(temp_workspace))
        result = await reg.execute(
            ToolCall(name="read_file", arguments={"path": "../../../etc/passwd"})
        )

        # Текстовый [BLOCKED] в output — независимо от success-флага
        # реестра (file-tool-ы конвертируют SecurityError в строку).
        assert "[BLOCKED]" in result.output

        history_after = list(diagnostics_bus._buffer)[history_before:]
        tool_events = [
            json.loads(line) for line in history_after
            if '"kind": "tool_execution"' in line
        ]
        assert any(ev.get("tool") == "read_file" for ev in tool_events), tool_events

    @pytest.mark.asyncio
    async def test_emits_on_unknown_tool(self, temp_workspace: Path) -> None:
        """Даже неизвестный tool эмитится в лог — это инцидент, должно быть видно."""
        from core.diagnostics import diagnostics_bus
        from core.models import ToolCall
        from tools.registry import ToolRegistry

        history_before = len(diagnostics_bus._buffer)

        reg = ToolRegistry(workspace=str(temp_workspace))
        result = await reg.execute(ToolCall(name="no_such_tool", arguments={}))

        assert not result.success
        history_after = list(diagnostics_bus._buffer)[history_before:]
        tool_events = [
            json.loads(line) for line in history_after
            if '"kind": "tool_execution"' in line
        ]
        assert any(ev.get("tool") == "no_such_tool" for ev in tool_events), tool_events

    @pytest.mark.asyncio
    async def test_delete_file_registered(self, temp_workspace: Path) -> None:
        """Реестр по умолчанию должен включать delete_file и search_in_file."""
        from tools.registry import ToolRegistry

        reg = ToolRegistry(workspace=str(temp_workspace))
        names = reg.list_names()
        assert "delete_file" in names
        assert "search_in_file" in names


# ───────────────────────────────────────────────────────────────────
# Agent.parse_json_tool_calls — поддержка <tool_call>
# ───────────────────────────────────────────────────────────────────
class TestParseToolCalls:
    def test_hermes_style_tool_call(self) -> None:
        from agents.base import Agent

        content = (
            "Сейчас прочитаю файл:\n"
            "<tool_call>\n"
            '{"name": "read_file", "arguments": {"path": "foo.txt"}}\n'
            "</tool_call>"
        )
        calls = Agent.parse_json_tool_calls(content)
        assert len(calls) == 1
        assert calls[0].name == "read_file"
        assert calls[0].arguments == {"path": "foo.txt"}

    def test_hermes_nested_arguments_string(self) -> None:
        """Hermes любит arguments как JSON-строку внутри объекта."""
        from agents.base import Agent

        inner = json.dumps({"command": "ls -la"})
        content = (
            f"<tool_call>\n"
            f'{{"name": "execute_bash", "arguments": {json.dumps(inner)}}}\n'
            f"</tool_call>"
        )
        calls = Agent.parse_json_tool_calls(content)
        assert len(calls) == 1
        assert calls[0].name == "execute_bash"
        # arguments должны быть распарсены в dict, а не остаться строкой
        assert isinstance(calls[0].arguments, dict)
        assert calls[0].arguments.get("command") == "ls -la"

    def test_cline_style_still_works(self) -> None:
        """Старый ```json-формат не должен сломаться."""
        from agents.base import Agent

        content = (
            "Пишу файл:\n"
            "```json\n"
            '{"name": "write_file", "arguments": {"path": "x.txt", "content": "hi"}}\n'
            "```\n"
        )
        calls = Agent.parse_json_tool_calls(content)
        assert len(calls) == 1
        assert calls[0].name == "write_file"
        assert calls[0].arguments["content"] == "hi"

    def test_no_tool_calls(self) -> None:
        from agents.base import Agent

        calls = Agent.parse_json_tool_calls("Просто текст без tool-ов.")
        assert calls == []

    def test_invalid_json_in_tool_call_skipped(self) -> None:
        """Невалидный JSON внутри <tool_call> не должен валить парсер."""
        from agents.base import Agent

        content = (
            "<tool_call>\n{not valid json}\n</tool_call>\n"
            '<tool_call>\n{"name": "list_dir", "arguments": {"path": "."}}\n</tool_call>'
        )
        calls = Agent.parse_json_tool_calls(content)
        # Только валидный блок должен пройти
        assert len(calls) == 1
        assert calls[0].name == "list_dir"
