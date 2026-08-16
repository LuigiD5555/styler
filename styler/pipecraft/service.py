"""Descubrimiento y supervisión ligera del servicio PipeCraft."""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from .client import PipeCraftClient, PipeCraftIpcError
from styler.runtime.commands import PipeCraftRunner

PIPECRAFT_VERSION = "1.5.0-alpha.1"


class PipeCraftUnavailable(RuntimeError):
    pass


def workspace_for(styler_root: Path) -> Path:
    return Path(styler_root) / ".styler" / "pipecraft"


def locate_binary() -> Path | None:
    configured = os.environ.get("PIPECRAFT_BIN")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(sys.executable).with_name("pipecraft"),
        Path(shutil.which("pipecraft")) if shutil.which("pipecraft") else None,
    ]
    for candidate in candidates:
        if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
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


def ensure_service(styler_root: Path, *, startup_timeout: float = 4.0) -> PipeCraftClient:
    workspace = prepare_workspace(styler_root)
    client = PipeCraftClient(workspace)
    try:
        info = client.ping()
        if str(info.get("protocol", "")) != "pipecraft.ipc/v1":
            raise PipeCraftUnavailable("El servicio PipeCraft usa un protocolo IPC incompatible.")
        return client
    except PipeCraftIpcError:
        pass

    binary = locate_binary()
    if binary is None:
        raise PipeCraftUnavailable(
            "No se encontró el binario Rust de PipeCraft 1.5. Instala Styler con cargo disponible, "
            "instala pipecraft en PATH o define PIPECRAFT_BIN."
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
            if str(info.get("protocol", "")) == "pipecraft.ipc/v1":
                return client
            last_error = "protocolo IPC incompatible"
        except PipeCraftIpcError as exc:
            last_error = str(exc)
        time.sleep(0.08)
    raise PipeCraftUnavailable(f"PipeCraft no quedó listo: {last_error or 'sin respuesta del servicio'}")
