"""Detección de la capa mínima usada para ejecutar Styler."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from styler import __version__
from styler.runtime.commands import PipeCraftRunner
from .models import RuntimeComponent, RuntimeProfile


def _version(command: list[str]) -> str:
    try:
        completed = PipeCraftRunner(timeout=5).run(command, timeout=5)
    except Exception:
        return ""
    text = completed.stdout.strip().splitlines()
    return text[0] if text else ""


def detect_runtime_profile() -> RuntimeProfile:
    conda_prefix = os.environ.get("CONDA_PREFIX", "")
    virtual_env = os.environ.get("VIRTUAL_ENV", "")
    if conda_prefix:
        env_provider = "conda"
        env_path = conda_prefix
    elif virtual_env:
        env_provider = "venv"
        env_path = virtual_env
    elif sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        env_provider = "venv"
        env_path = sys.prefix
    else:
        env_provider = "system"
        env_path = sys.prefix

    rustc = shutil.which("rustc") or ""
    cargo = shutil.which("cargo") or ""
    rust_version = _version([rustc, "--version"]) if rustc else ""
    rust_provider = "toolchain" if rustc else "bundled-or-not-required"
    rust_source = cargo or rustc

    return RuntimeProfile(
        python=RuntimeComponent(
            provider="system" if env_provider == "system" else env_provider,
            version=".".join(str(part) for part in sys.version_info[:3]),
            executable=str(Path(sys.executable).resolve()),
            source=sys.prefix,
            supplied_by_distro=env_provider == "system",
        ),
        environment=RuntimeComponent(
            provider=env_provider,
            version="",
            executable=env_path,
            source=env_path,
            supplied_by_distro=env_provider == "system",
        ),
        rust=RuntimeComponent(
            provider=rust_provider,
            version=rust_version,
            executable=rustc,
            source=rust_source,
            supplied_by_distro=False,
        ),
        styler=RuntimeComponent(
            provider="python-package",
            version=__version__,
            executable=shutil.which("styler") or "",
            source=str(Path(__file__).resolve().parents[1]),
            supplied_by_distro=False,
        ),
    )
