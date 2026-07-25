"""
tools/bash_tool.py
──────────────────
ExecuteBash — запуск shell-команд.

Безопасность:
  • Работаем только в пределах workspace_dir.
  • Блокируем самые опасные паттерны (rm -rf /, dd, mkfs, …).
  • Таймаут на выполнение (по умолчанию 60 секунд).
  • stdout/stderr захватываются и склеиваются в одну строку.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import Any, Dict

from tools.base import Tool

# Чёрный список — при наличии в команде сразу отказ
_DENY_PATTERNS = [
    r"rm\s+-rf\s+/\s*$",
    r"rm\s+-rf\s+/\*",
    r":\(\)\s*\{.*\}\s*;:",  # fork bomb
    r"mkfs",
    r"dd\s+if=.*of=/dev/",
    r">\s*/dev/sd[a-z]",
    r"format\s+c:",
]
_DENY_RE = re.compile("|".join(_DENY_PATTERNS), re.IGNORECASE)


class ExecuteBash(Tool):
    name = "execute_bash"
    description = (
        "Run a shell command in the workspace. Use PowerShell on Windows, "
        "bash on Linux/Mac. The command runs in the workspace directory. "
        "Returns combined stdout+stderr."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 60, max 300).",
                "default": 60,
                "minimum": 1,
                "maximum": 300,
            },
        },
        "required": ["command"],
    }

    def __init__(self, workspace: str = "."):
        self.workspace = os.path.abspath(workspace)

    async def execute(self, arguments: Dict[str, Any]) -> str:
        command: str = arguments["command"]
        timeout: int = int(arguments.get("timeout", 60))

        # ── Проверка безопасности ─────────────────────────────────
        if _DENY_RE.search(command):
            return "[BLOCKED] Command matches deny-list (potentially destructive)."

        # Выбор шелла: PowerShell на Windows, иначе bash
        if sys.platform.startswith("win"):
            shell_cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            shell_cmd = ["/bin/bash", "-lc", command]

        # ── Запуск ────────────────────────────────────────────────
        try:
            proc = await asyncio.create_subprocess_exec(
                *shell_cmd,
                cwd=self.workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return f"[TIMEOUT] Command exceeded {timeout}s and was killed."
        except FileNotFoundError as e:
            return f"[ERROR] Shell not found: {e}"
        except Exception as e:  # noqa: BLE001
            return f"[ERROR] Failed to start process: {e}"

        out = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")
        rc = proc.returncode
        result = out
        if err:
            result += ("\n--- stderr ---\n" + err)
        result += f"\n[exit code: {rc}]"
        
        # Умное усечение: оставляем начало и конец, если строк слишком много
        lines = result.splitlines()
        if len(lines) > 200:
            head = lines[:100]
            tail = lines[-100:]
            result = "\n".join(head) + f"\n\n[... {len(lines)-200} lines truncated ...]\n\n" + "\n".join(tail)

        # Ограничим общую длину в символах (защита от гигантских однострочников)
        if len(result) > 20_000:
            result = result[:10_000] + f"\n\n[... {len(result)-20_000} chars truncated ...]\n\n" + result[-10_000:]
            
        return result
