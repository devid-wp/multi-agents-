"""
tools/exceptions.py
───────────────────
Исключения уровня инструментов.

SecurityError — единый маркер «попытка нарушить sandbox» (выход за пределы
workspace_dir, запрещённый паттерн, удаление защищённого пути).

Наследуемся от PermissionError, чтобы существующие обработчики
`except PermissionError` в tools/registry.py и tools/file_tool.py
продолжали ловить SecurityError без изменений.
"""


class SecurityError(PermissionError):
    """
    Попытка нарушить политику безопасности sandbox-а.

    Кидается из tools/* в одном из трёх случаев:
      • путь резолвится ЗА пределы workspace_dir;
      • shell-команда матчится с deny-list (rm -rf /, fork-bomb и т. п.);
      • попытка удалить защищённый файл (core/main/...).

    В Agent.run()/ToolRegistry.execute() SecurityError ловится тем же
    блоком, что и PermissionError, и превращается в ToolResult со
    success=False и error="Permission denied: ...". Дополнительно
    в `tool_execution`-событии шины фиксируется, что путь был заблокирован.
    """

    def __init__(self, message: str = "Security violation"):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:  # явный override — без него `str(exc)` теряет текст
        return self.message


class ToolValidationError(ValueError):
    """
    Невалидные аргументы инструмента (несоответствие JSON-схеме).
    """
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


__all__ = ["SecurityError", "ToolValidationError"]

