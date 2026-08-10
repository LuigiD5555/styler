"""Perfiles declarativos de preparación de aplicaciones."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .conditions import AllCondition, CallableCondition, Condition, ProcessRunningCondition


@dataclass(frozen=True)
class ApplicationProfile:
    id: str
    process_name: str
    readiness_checks: tuple[Condition, ...]
    startup_timeout_seconds: float = 20.0
    poll_interval_seconds: float = 0.1
    settle_seconds: float = 0.0
    minimum_runtime_seconds: float = 0.0
    process_condition: Condition | None = None

    def fully_loaded_condition(self) -> AllCondition:
        process_check = self.process_condition or ProcessRunningCondition(self.process_name)
        return AllCondition(
            f"{self.id} completamente cargada",
            (process_check, *self.readiness_checks),
        )


def callback_check(
    name: str,
    predicate: Callable[[], bool],
    detail: Callable[[], str] | None = None,
) -> CallableCondition:
    """Adapter para futuras comprobaciones AT-SPI, ventana, imagen o plugin."""

    return CallableCondition(name, predicate, detail)
