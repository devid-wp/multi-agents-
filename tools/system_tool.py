"""
tools/system_tool.py
────────────────────
Минимальный «read-only» инструмент для диагностики окружения.

Идея: дать агенту безопасный способ узнать базовые факты о среде
(платформа, версия Python, текущее время, текущая директория),
НЕ давая ему при этом произвольного shell-доступа. Это первый
tool, который регистрируется рядом с file/bash, чтобы пройти
полный цикл Tool Call без рисков для системы.
"""

from __future__ import annotations

import platform
import sys
from datetime import datetime, timezone
from typing import Any, Dict

from tools.base import Tool


class GetSystemStatus(Tool):
    """
    Возвращает короткий текстовый отчёт о среде.

    Нет параметров → JSON-схема `properties: {}` (не `None`!),
    иначе OpenAI-совместимые провайдеры падают на валидации tools=[...].

    Безопасность: инструмент НЕ принимает путей, команд, env-имён.
    Возвращает только предопределённый набор полей. Это by design —
    даже если LLM попросит `{"include": "everything"}`, схема
    отрежет extra-аргументы на стороне провайдера, а на нашей стороне
    мы их просто игнорируем.
    """

    name = "get_system_status"
    description = (
        "Returns a short, read-only report about the runtime environment: "
        "platform, Python version, current UTC time, and process CWD. "
        "Use this when you need to confirm where your code is running "
        "before making filesystem or shell decisions."
    )
    parameters_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def execute(self, arguments: Dict[str, Any]) -> str:
        # arguments игнорируем полностью: ни параметров, ни override-ов.
        # Это защищает от попыток LLM «расширить» схему.
        report = {
            "platform": platform.platform(),
            "python": sys.version.split()[0],  # только "3.x.y"
            "cwd": _safe_cwd(),
            "utc_now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        # Текстовый формат — LLM парсит человеческим глазом проще, чем JSON
        # в одну строку, плюс легче копировать в логи.
        lines = [f"{k}: {v}" for k, v in report.items()]
        return "\n".join(lines)


def _safe_cwd() -> str:
    """
    Возвращает текущую рабочую директорию.

    В отличие от file-tool-ов, здесь НЕТ sandbox-проверки: это просто
    метка окружения. Если по какой-то причине cwd не читается
    (например, директорию удалили) — отдаём явный placeholder,
    а не падаем.
    """
    try:
        import os
        return os.getcwd()
    except OSError:
        return "<unavailable>"


__all__ = ["GetSystemStatus"]
