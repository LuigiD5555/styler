"""Descubrimiento y supervisión ligera del servicio PipeCraft separado.

Desde Styler 0.11.0 el source de PipeCraft no forma parte del repositorio
de Styler y no existe fallback productivo a un scheduler Python. PipeCraft es una
dependencia de runtime independiente y la única autoridad para ejecutar DAGs.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
import time
from pathlib import Path

from styler.runtime.commands import PipeCraftRunner

from .client import PipeCraftClient, PipeCraftIpcError
from .contract import IPC_PROTOCOL, MIN_VERSION, check_runtime

PIPECRAFT_VERSION = MIN_VERSION


class PipeCraftUnavailable(RuntimeError):
    pass


def workspace_for(styler_root: Path) -> Path:
    return Path(styler_root) / ".styler" / "pipecraft"


def _bundled_binary() -> Path | None:
    """Return the architecture-specific runtime shipped in a Styler release."""
    machine = platform.machine().lower()
    arch = {
        "x86_64": "linux-x86_64",
        "amd64": "linux-x86_64",
        "aarch64": "linux-aarch64",
        "arm64": "linux-aarch64",
    }.get(machine)
    if not arch:
        return None
    project_root = Path(__file__).resolve().parents[2]
    candidate = project_root / "runtime" / "pipecraft" / arch / "pipecraft"
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def locate_binary() -> Path | None:
    configured = os.environ.get("PIPECRAFT_BIN")
    found = shutil.which("pipecraft")
    candidates = [
        Path(configured).expanduser() if configured else None,
        # 0.11: la distribución oficial incluye un runtime privado por arquitectura.
        _bundled_binary(),
        # La instalación atómica copia el runtime privado junto al Python del venv.
        Path(sys.executable).with_name("pipecraft"),
        # Paquetes de sistema mantienen el runtime como implementación privada.
        Path(sys.prefix) / "libexec" / "styler" / "pipecraft",
        Path("/usr/libexec/styler/pipecraft"),
        Path(found) if found else None,
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def prepare_workspace(styler_root: Path) -> Path:
    workspace = workspace_for(styler_root)
    pipeline_dir = workspace / ".pipelines" / "pipelines"
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    config = workspace / ".pipelines" / "workspace.yaml"
    if not config.exists():
        config.write_text(
            "workspace:\n"
            "  name: styler\n"
            "  description: Runtime privado de Styler\n"
            "paths:\n"
            "  pipeline_dir: .pipelines/pipelines\n"
            "  runs_dir: .pipelines/runs\n"
            "  outputs_dir: .pipelines/out\n",
            encoding="utf-8",
        )
    return workspace


def _validate_info(info: dict) -> None:
    version = str(info.get("version", ""))
    protocol = str(info.get("protocol", ""))
    compatibility = check_runtime(version, protocol)
    if not compatibility.compatible:
        raise PipeCraftUnavailable(compatibility.reason)


def diagnose(styler_root: Path) -> dict[str, str | bool]:
    """Diagnóstico sin arrancar el daemon ni producir efectos."""
    workspace = workspace_for(styler_root)
    binary = locate_binary()
    result: dict[str, str | bool] = {
        "binary_available": bool(binary),
        "binary": str(binary or ""),
        "required_version": MIN_VERSION,
        "required_protocol": IPC_PROTOCOL,
        "service_active": False,
        "service_version": "",
        "service_protocol": "",
        "compatible": False,
        "message": "",
    }
    try:
        info = PipeCraftClient(workspace).ping()
        result["service_active"] = True
        result["service_version"] = str(info.get("version", ""))
        result["service_protocol"] = str(info.get("protocol", ""))
        compatibility = check_runtime(result["service_version"], result["service_protocol"])
        result["compatible"] = compatibility.compatible
        result["message"] = compatibility.reason
    except Exception as exc:
        result["message"] = str(exc)
    return result


def ensure_service(styler_root: Path, *, startup_timeout: float = 4.0) -> PipeCraftClient:
    workspace = prepare_workspace(styler_root)
    client = PipeCraftClient(workspace)
    try:
        info = client.ping()
        _validate_info(info)
        return client
    except PipeCraftUnavailable:
        # Un daemon incompatible no debe ser reemplazado silenciosamente por
        # otro proceso contra el mismo workspace/socket.
        raise
    except PipeCraftIpcError:
        pass

    binary = locate_binary()
    if binary is None:
        raise PipeCraftUnavailable(
            f"PipeCraft >= {MIN_VERSION} no está disponible para esta arquitectura. "
            "La distribución oficial de Styler 0.11 debe incluir su runtime privado. "
            "Como alternativa de desarrollo, instala PipeCraft en PATH o define PIPECRAFT_BIN."
        )

    runtime_dir = workspace / ".pipelines" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        PipeCraftRunner().spawn_background_logged(
            [str(binary), "--root", str(workspace), "serve", "--recovery", "manual"],
            runtime_dir / "service.log",
            cwd=workspace,
        )
    except OSError as exc:
        raise PipeCraftUnavailable(f"No se pudo iniciar PipeCraft: {exc}") from exc

    deadline = time.monotonic() + startup_timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            info = client.ping()
            _validate_info(info)
            return client
        except (PipeCraftIpcError, PipeCraftUnavailable) as exc:
            last_error = str(exc)
        time.sleep(0.08)
    raise PipeCraftUnavailable(f"PipeCraft no quedó listo: {last_error or 'sin respuesta del servicio'}")
