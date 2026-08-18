"""Contrato mínimo para ejecutar comandos fuera del runtime DAG."""
from __future__ import annotations

from typing import Protocol


class CommandExecutor(Protocol):
    def available(self, program: str) -> bool: ...
    def run(self, argv: list[str], timeout: float | None = None): ...
