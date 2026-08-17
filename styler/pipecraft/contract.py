"""Contrato de runtime entre Styler y PipeCraft.

Styler no vendoriza ni reimplementa PipeCraft. Este módulo contiene únicamente
los requisitos mínimos necesarios para negociar con un runtime externo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

IPC_PROTOCOL = "pipecraft.ipc/v1"
MIN_VERSION = "1.5.0-alpha.1"
SUPPORTED_MAJOR = 1
SUPPORTED_MINOR_MIN = 5


@dataclass(frozen=True)
class RuntimeCompatibility:
    compatible: bool
    reason: str = ""


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)", str(value or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def check_runtime(version: str, protocol: str) -> RuntimeCompatibility:
    if protocol != IPC_PROTOCOL:
        return RuntimeCompatibility(False, f"protocolo incompatible: {protocol or 'desconocido'}")
    parsed = _version_tuple(version)
    if parsed is None:
        return RuntimeCompatibility(False, f"versión PipeCraft inválida: {version or 'desconocida'}")
    major, minor, _patch = parsed
    if major != SUPPORTED_MAJOR or minor < SUPPORTED_MINOR_MIN:
        return RuntimeCompatibility(False, f"Styler requiere PipeCraft >= {MIN_VERSION} dentro de la serie 1.x")
    return RuntimeCompatibility(True)
