"""Errores y progreso compartidos por núcleo, CLI y TUI."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class ProgressEvent:
    stage: str
    current: int
    total: int
    message: str


ProgressCallback = Optional[Callable[[ProgressEvent], None]]


@dataclass
class UserError(Exception):
    message: str
    detail: str = ""

    def __str__(self) -> str:
        return self.message


class AuthorizationError(UserError):
    """La fase administrativa no obtuvo autorización."""


class EnvironmentRestoreError(UserError):
    """Un requisito obligatorio no quedó instalado o verificado."""


class OperationCancelledError(UserError):
    """La persona canceló una operación y el sistema fue recogido."""
