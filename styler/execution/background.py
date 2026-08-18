"""Arranque de procesos auxiliares fuera de la frontera IPC de PipeCraft.

Esto sólo se usa para iniciar el daemon PipeCraft cuando Styler lo empaqueta
como runtime privado. Los comandos de los DAG nunca pasan por aquí.
"""
from __future__ import annotations

from pathlib import Path

from .processes import ProcessRunner


def spawn_background_logged(argv: list[str], log_path: Path, *, cwd: Path | None = None) -> int:
    process = ProcessRunner().spawn_background_logged(argv, log_path, cwd=cwd)
    return int(process.pid)
