"""
tools/git_tool.py
─────────────────
GitTool — безопасный инструмент для git-операций.

Разрешены только базовые операции из белого списка.
Запрещены shell-пайпы и произвольные бинарники.
shell=False гарантирует защиту от инъекций.
"""
import asyncio
import shlex
from typing import Any, Dict

from tools.base import Tool
from tools.exceptions import SecurityError, ToolExecutionError


class GitTool(Tool):
    """
    Инструмент для безопасного выполнения git-команд в рабочей директории (workspace).
    Разрешены только базовые операции, не требующие интерактивности.
    Запрещены shell-пайпы и произвольные бинарники.
    """

    name = "execute_git"
    description = (
        "Выполняет git команду (например, 'status', 'diff', 'commit -m \"msg\"', 'push', 'add .'). "
        "Автоматически подставляет 'git' перед аргументами. Не поддерживает shell-пайпы (|) и перенаправления."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Git subcommand and arguments (without 'git'). Example: 'commit -m \"fix: update\"'",
            }
        },
        "required": ["command"],
    }

    # Белый список безопасных подкоманд git
    ALLOWED_SUBCOMMANDS = {
        "status", "diff", "add", "commit", "push", "pull",
        "checkout", "log", "branch", "reset", "restore",
        "rm", "mv", "fetch", "stash", "show", "tag"
    }

    def __init__(self, workspace: str = "."):
        import os
        self.workspace_dir = os.path.abspath(workspace)

    async def execute(self, arguments: Dict[str, Any]) -> str:
        command: str = arguments.get("command", "")

        if not command or not command.strip():
            raise ToolExecutionError("execute_git: пустая команда.")

        try:
            args = shlex.split(command)
        except ValueError as e:
            raise ToolExecutionError(f"execute_git: ошибка разбора команды: {e}")

        # Удаляем 'git', если агент случайно передал его
        if args and args[0] == "git":
            args.pop(0)

        if not args:
            raise ToolExecutionError("execute_git: пустая команда после 'git'.")

        subcommand = args[0]
        if subcommand not in self.ALLOWED_SUBCOMMANDS:
            raise SecurityError(
                f"execute_git: подкоманда '{subcommand}' запрещена. "
                f"Разрешены: {', '.join(sorted(self.ALLOWED_SUBCOMMANDS))}"
            )

        git_cmd = ["git"] + args

        try:
            # shell=False — защита от шелл-инъекций
            proc = await asyncio.create_subprocess_exec(
                *git_cmd,
                cwd=self.workspace_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            output_parts = []
            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))
            if stderr:
                output_parts.append(stderr.decode("utf-8", errors="replace"))

            output = "\n".join(output_parts).strip()

            if proc.returncode != 0:
                return f"[git exit code {proc.returncode}]\n{output}" if output else f"[git exit code {proc.returncode}]"

            return output or "Команда выполнена успешно, вывод пуст."

        except asyncio.TimeoutError:
            raise ToolExecutionError("execute_git: превышен таймаут 30 секунд.")
        except FileNotFoundError:
            raise ToolExecutionError("execute_git: git не найден. Убедитесь что git установлен и доступен в PATH.")
        except Exception as e:
            raise ToolExecutionError(f"execute_git: ошибка при выполнении {git_cmd}: {e}")
