"""Contrato de runtime entre Styler y PipeCraft."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

IPC_PROTOCOL = "pipecraft.ipc/v1"
LEGACY_MIN_VERSION = "1.5.0-alpha.1"
MIN_VERSION = "1.6.0-alpha.1"
SUPPORTED_MAJOR = 1
REQUIRED_SPEC_CAPABILITIES = frozenset({"submit_spec", "validate_spec", "plan_spec"})


@dataclass(frozen=True)
class RuntimeCompatibility:
    compatible: bool
    reason: str = ""
    legacy: bool = False
    missing_capabilities: tuple[str, ...] = ()


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)", str(value or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def check_runtime(
    version: str,
    protocol: str,
    capabilities: Iterable[str] = (),
    *,
    allow_legacy: bool = True,
) -> RuntimeCompatibility:
    if protocol != IPC_PROTOCOL:
        return RuntimeCompatibility(False, f"protocolo incompatible: {protocol or 'desconocido'}")
    parsed = _version_tuple(version)
    if parsed is None:
        return RuntimeCompatibility(False, f"versión PipeCraft inválida: {version or 'desconocida'}")
    major, minor, _patch = parsed
    if major != SUPPORTED_MAJOR:
        return RuntimeCompatibility(False, f"Styler requiere PipeCraft 1.x; recibido {version}")

    caps = {str(value) for value in capabilities}
    missing = tuple(sorted(REQUIRED_SPEC_CAPABILITIES - caps))
    if minor >= 6 and not missing:
        return RuntimeCompatibility(True)
    if allow_legacy and minor >= 5:
        reason = (
            "runtime legado compatible: se usará el adaptador YAML de PipeCraft 1.5; "
            f"para la frontera IPC directa usa PipeCraft >= {MIN_VERSION} con capacidades "
            + ", ".join(sorted(REQUIRED_SPEC_CAPABILITIES))
        )
        return RuntimeCompatibility(True, reason, legacy=True, missing_capabilities=missing)
    if minor >= 6 and missing:
        return RuntimeCompatibility(
            False,
            "PipeCraft no anuncia las capacidades requeridas: " + ", ".join(missing),
            missing_capabilities=missing,
        )
    return RuntimeCompatibility(False, f"Styler requiere PipeCraft >= {LEGACY_MIN_VERSION} para compatibilidad")
