"""
tools/base.py
─────────────
Базовый класс Tool — аналог Cline ToolDefinition.

Каждый инструмент должен:
  • иметь уникальное name (snake_case)
  • иметь description (объясняет LLM, когда использовать)
  • иметь JSON-схему параметров (для tool-calling)
  • реализовать async execute(arguments: dict) → str
"""

from __future__ import annotations

import abc
from typing import Any, Dict


class Tool(abc.ABC):
    """Интерфейс инструмента Trinity."""

    name: str = ""
    description: str = ""
    parameters_schema: Dict[str, Any] = {}

    @abc.abstractmethod
    async def execute(self, arguments: Dict[str, Any]) -> str:
        """
        Выполнить инструмент.
        :param arguments: аргументы от LLM (уже провалидированные JSON-схемой).
        :return: текстовый результат (stdout, содержимое файла, и т.п.).
        """
        raise NotImplementedError

    def to_openai_schema(self) -> Dict[str, Any]:
        """OpenAI/NVIDIA-совместимая схема для передачи в tools=[...]."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }
