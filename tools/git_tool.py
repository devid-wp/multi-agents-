import os
import subprocess
import asyncio
from typing import Optional, List
from .base import Tool
from .exceptions import SecurityError, ToolExecutionError

class GitTool(Tool):
    """
    Инструмент для безопасного выполнения git-команд в рабочей директории (workspace).
    Разрешены только базовые операции, не требующие интерактивности (commit, push, diff, status и т.д.).
    Запрещены shell-пайпы и произвольные бинарники.
    """
    
    name = "execute_git"
    description = (
        "Выполняет git команду (например, 'status', 'diff', 'commit -m \"msg\"', 'push', 'add .'). "
        "Автоматически подставляет 'git' перед аргументами. Не поддерживает shell-пайпы (|) и перенаправления."
    )
    
    # Белый список безопасных подкоманд git
    ALLOWED_SUBCOMMANDS = {
        "status", "diff", "add", "commit", "push", "pull", 
        "checkout", "log", "branch", "reset", "restore", "rm", "mv", "fetch", "stash"
    }

    async def execute(self, command: str) -> str:
        if not self.workspace_dir:
            raise ToolExecutionError("execute_git: workspace_dir не задан.")
            
        import shlex
        args = shlex.split(command)
        if not args:
            raise ToolExecutionError("execute_git: пустая команда.")
            
        # Удаляем 'git', если агент случайно передал его
        if args[0] == "git":
            args.pop(0)
            
        if not args:
            raise ToolExecutionError("execute_git: пустая команда после 'git'.")
            
        subcommand = args[0]
        if subcommand not in self.ALLOWED_SUBCOMMANDS:
            raise SecurityError(f"execute_git: подкоманда '{subcommand}' запрещена.")
            
        git_cmd = ["git"] + args
        
        try:
            # Выполняем асинхронно
            # shell=False гарантирует безопасность от шелл-инъекций
            proc = await asyncio.create_subprocess_exec(
                *git_cmd,
                cwd=self.workspace_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await proc.communicate()
            
            output = ""
            if stdout:
                output += stdout.decode('utf-8', errors='replace')
            if stderr:
                output += stderr.decode('utf-8', errors='replace')
                
            if proc.returncode != 0:
                output = f"Команда завершилась с ошибкой (код {proc.returncode}):\n{output}"
                
            return output.strip() or "Команда выполнена успешно, но не вернула вывода."
            
        except Exception as e:
            raise ToolExecutionError(f"execute_git: ошибка при выполнении {git_cmd}: {e}")
