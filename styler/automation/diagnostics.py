"""Diagnóstico guardado cuando una espera no se cumple.

Cuando `wait_until` vence, el mensaje de error por sí solo no basta para saber
qué pasó: hace falta saber qué subcondición seguía sin cumplirse, si el proceso
seguía vivo y qué había en el directorio observado. Este módulo escribe ese
paquete en disco y devuelve su ubicación, para que la interfaz pueda decir
exactamente dónde mirar en vez de pedirle al usuario que reproduzca el fallo.
"""
from __future__ import annotations

import json
import os
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .conditions import Condition, ConditionState, WaitResult

DIAGNOSTICS_ROOT = ".styler/diagnostics"


@dataclass(frozen=True)
class DiagnosticBundle:
    path: Path
    summary: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def location(self) -> str:
        return str(self.path)


def _condition_tree(condition: Condition) -> dict[str, Any]:
    """Describe el último snapshot sin reevaluar la condición.

    Un diagnóstico es evidencia, no otra ejecución. Si la condición nunca fue
    evaluada se declara explícitamente en vez de lanzar procesos para rellenar
    el reporte.
    """
    state = str(getattr(condition, "_styler_last_state", "not_evaluated"))
    detail = str(getattr(condition, "_styler_last_diagnostic", "sin snapshot"))
    node: dict[str, Any] = {
        "name": getattr(condition, "name", type(condition).__name__),
        "type": type(condition).__name__,
        "state": state,
        "diagnostic": detail,
    }
    children = getattr(condition, "conditions", None)
    if children:
        node["children"] = [_condition_tree(child) for child in children]
    inner = getattr(condition, "condition", None)
    if inner is not None and inner is not condition:
        node["inner"] = _condition_tree(inner)
    return node


def _directory_listing(path: Path, limit: int = 60) -> list[str]:
    if not path.is_dir():
        return []
    entries: list[str] = []
    for index, item in enumerate(sorted(path.iterdir())):
        if index >= limit:
            entries.append(f"… ({index}+ entradas)")
            break
        entries.append(item.name + ("/" if item.is_dir() else ""))
    return entries


def capture_wait_failure(
    result: WaitResult,
    *,
    root: str | Path,
    scope: str,
    condition: Condition | None = None,
    observed_paths: Sequence[str | Path] = (),
    logs: Mapping[str, str] | None = None,
    extra: Mapping[str, Any] | None = None,
    clock: Any = time.time,
) -> DiagnosticBundle:
    """Guarda el diagnóstico de una espera fallida y devuelve su ubicación."""

    stamp = int(clock())
    folder = Path(root) / DIAGNOSTICS_ROOT / f"{stamp}-{scope.replace('/', '-')}"
    folder.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "scope": scope,
        "captured_at": stamp,
        "wait": {
            "condition": result.condition,
            "satisfied": result.satisfied,
            "reason": result.reason,
            "elapsed_seconds": result.elapsed_seconds,
            "attempts": result.attempts,
            "diagnostic": result.diagnostic,
        },
        "environment": {
            # `platform.platform()` lanza un subproceso para averiguar el
            # procesador; capturar un diagnóstico nunca debe ejecutar nada.
            "platform": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "session_type": os.environ.get("XDG_SESSION_TYPE", ""),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
            "display": bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
        },
    }
    if condition is not None:
        payload["condition_tree"] = _condition_tree(condition)
    if observed_paths:
        payload["observed_paths"] = {
            str(item): {
                "exists": Path(item).exists(),
                "listing": _directory_listing(Path(item)),
            }
            for item in observed_paths
        }
    if extra:
        payload["extra"] = dict(extra)

    (folder / "diagnostic.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    for name, content in (logs or {}).items():
        safe = name.replace("/", "-")
        (folder / f"{safe}.log").write_text(content, encoding="utf-8")

    pending = _pending_names(payload.get("condition_tree"))
    summary = (
        f"La espera «{result.condition}» terminó por {result.reason} tras "
        f"{result.elapsed_seconds:.1f} s"
    )
    if pending:
        summary += f"; seguía sin cumplirse: {', '.join(pending)}"
    summary += f". Diagnóstico en {folder / 'diagnostic.json'}"
    return DiagnosticBundle(folder / "diagnostic.json", summary, payload)


def _pending_names(node: Any) -> list[str]:
    if not isinstance(node, dict):
        return []
    children = node.get("children") or []
    names: list[str] = []
    for child in children:
        names.extend(_pending_names(child))
    if not children and node.get("state") != ConditionState.SATISFIED.value:
        names.append(str(node.get("name")))
    return names
