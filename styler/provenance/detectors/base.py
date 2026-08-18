"""
styler.provenance.detectors.base
================================
Un Detector responde una sola pregunta: «de dónde salieron las aplicaciones
que administra este gestor». Nunca instala, nunca descarga, nunca escribe.

Todo comando externo pasa por un `CommandRunner` inyectable. Eso permite
probar los detectores sin APT, sin Flatpak y sin red.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Callable, Protocol

from styler.provenance.models import ApplicationRecord
from styler.execution.processes import ProcessRunner

DEFAULT_TIMEOUT = 30


class CommandError(RuntimeError):
    pass


@dataclass
class CommandRunner:
    """Ejecuta comandos de solo lectura y devuelve stdout."""

    timeout: int = DEFAULT_TIMEOUT
    which: Callable[[str], str | None] = field(default=shutil.which)

    def available(self, program: str) -> bool:
        return bool(self.which(program))

    def run(self, argv: list[str]) -> str:
        completed = ProcessRunner(timeout=self.timeout).run(argv, timeout=self.timeout)
        if completed.returncode != 0 and not completed.stdout:
            raise CommandError(
                f"{argv[0]} terminó con código {completed.returncode}: "
                f"{completed.stderr.strip()[:200]}"
            )
        return completed.stdout



class Runner(Protocol):
    def available(self, program: str) -> bool: ...
    def run(self, argv: list[str]) -> str: ...


class Detector:
    """Base de todos los detectores de procedencia."""

    name = "base"
    manager = "base"

    def __init__(self, runner: Runner | None = None) -> None:
        self.runner: Runner = runner or CommandRunner()
        self.problems: list[str] = []

    def applies(self) -> bool:
        """¿Este gestor existe en la máquina?"""
        return False

    def detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        """Devuelve los registros de procedencia. Nunca lanza excepción."""
        if not self.applies():
            return []
        try:
            return self._detect(scope=scope)
        except Exception as exc:  # noqa: BLE001 — un gestor roto no tumba el resto
            self.problems.append(f"{self.name}: {exc}")
            return []

    def _detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        raise NotImplementedError
