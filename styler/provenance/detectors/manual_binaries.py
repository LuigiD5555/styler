"""
styler.provenance.detectors.manual_binaries
===========================================
Lo que se instaló sin gestor: ``curl | bash``, un release de GitHub
descomprimido a mano, un binario suelto copiado a ``~/.local/bin``.

Método:

1. Recorrer un conjunto cerrado de directorios de binarios. **Nunca** el HOME
   completo ni el sistema de archivos entero.
2. Intentar que el gestor de la distribución reclame cada ejecutable
   (``dpkg -S``, ``pacman -Qo``, ``rpm -qf``). Si alguno lo reclama, se
   descarta: ya lo reportó su detector.
3. Lo que nadie reclama se registra con confianza ``UNKNOWN``.

Este detector **no adivina** de dónde vino un binario. No deduce el repositorio
de GitHub por el nombre del archivo, no consulta la red y no ejecuta el binario
para preguntarle su versión. Solo afirma lo que puede probar: que existe un
ejecutable en esa ruta y que ningún gestor lo reclama.

Es el único detector cuyo ``applies()`` es siempre verdadero, así que corre en
todas las máquinas. Por eso el recorrido está acotado y no sigue enlaces
simbólicos.
"""
from __future__ import annotations

import os
from pathlib import Path

from styler.provenance.detectors.base import Detector, Runner
from styler.provenance.models import (
    AppCategory,
    ApplicationRecord,
    Confidence,
    Integrity,
    InstallReason,
    Origin,
    OriginKind,
)

DEFAULT_BIN_DIRS = (
    "~/.local/bin",
    "~/bin",
    "/usr/local/bin",
    "/opt",
)
MAX_DEPTH = 2
MAX_ENTRIES = 400

# Nombres que casi siempre son parte del arranque de Styler o de su intérprete.
# No son "cambios del usuario" y ensucian la lista de detectados.
IGNORED_NAMES = frozenset(
    {
        "python", "python3", "pip", "pip3", "activate", "conda", "mamba",
        "rustc", "cargo", "rustup", "styler",
    }
)


class ManualBinaryDetector(Detector):
    name = "manual-binaries"
    manager = "manual"

    def __init__(
        self,
        runner: Runner | None = None,
        directories: tuple[str, ...] = DEFAULT_BIN_DIRS,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        super().__init__(runner)
        self.directories = directories
        self.max_entries = max_entries

    def applies(self) -> bool:
        return True

    def _detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        records: list[ApplicationRecord] = []
        seen: set[str] = set()

        for entry in self._candidates():
            if len(records) >= self.max_entries:
                self.problems.append(
                    "manual-binaries: se alcanzó el límite de entradas; "
                    "la lista puede estar incompleta."
                )
                break
            name = entry.name
            if name in IGNORED_NAMES or name in seen:
                continue
            owner = self._claimed_by(entry)
            if owner:
                continue
            seen.add(name)
            records.append(
                ApplicationRecord(
                    app_id=f"manual:{name}",
                    name=name,
                    display_name=name,
                    manager="manual",
                    install_method="manual",
                    install_reason=InstallReason.LOCAL,
                    category=AppCategory.UNKNOWN,
                    origin=Origin(
                        kind=OriginKind.MANUAL,
                        confidence=Confidence.UNKNOWN,
                        evidence=f"ejecutable en {entry.parent} sin gestor que lo reclame",
                    ),
                    integrity=Integrity(
                        artifact_path=str(entry),
                        artifact_available=True,
                    ),
                    warnings=[
                        "Instalado fuera de cualquier gestor de paquetes. Styler no "
                        "sabe de dónde vino ni puede volver a descargarlo; solo "
                        "conoce el archivo que está en disco."
                    ],
                )
            )
        return records

    def _candidates(self) -> list[Path]:
        found: list[Path] = []
        for raw in self.directories:
            root = Path(os.path.expanduser(raw))
            if not root.is_dir():
                continue
            found.extend(_walk_executables(root, MAX_DEPTH))
        return sorted(found, key=lambda path: path.name)

    def _claimed_by(self, path: Path) -> str:
        """Nombre del paquete que reclama la ruta, o cadena vacía."""
        probes = (
            ("dpkg", ["dpkg", "-S", str(path)]),
            ("pacman", ["pacman", "-Qo", str(path)]),
            ("rpm", ["rpm", "-qf", str(path)]),
        )
        for program, argv in probes:
            if not self.runner.available(program):
                continue
            try:
                out = self.runner.run(argv)
            except Exception:  # noqa: BLE001 — «no encontrado» sale por error
                continue
            text = out.strip()
            if text and "no path found" not in text and "not owned" not in text:
                return text.splitlines()[0]
        return ""


def _walk_executables(root: Path, depth: int) -> list[Path]:
    """Ejecutables regulares hasta ``depth`` niveles. No sigue symlinks."""
    if depth < 0:
        return []
    results: list[Path] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                results.extend(_walk_executables(entry, depth - 1))
            elif entry.is_file() and os.access(entry, os.X_OK):
                results.append(entry)
        except OSError:
            continue
    return results
