"""Selector opcional del único formato público de Styler: ``.stylerpkg``."""
from __future__ import annotations

import shutil
from pathlib import Path
from styler.runtime.commands import PipeCraftRunner


def _run(command: list[str]) -> str:
    result = PipeCraftRunner(timeout=120).run(command, timeout=120)
    return result.stdout.strip() if result.returncode == 0 else ""


def choose_portable_package_file(start: str | Path | None = None) -> str:
    start_path = str(Path(start or Path.home()).expanduser())
    if shutil.which("kdialog"):
        return _run(["kdialog", "--getopenfilename", start_path,
                     "Paquetes de Styler (*.stylerpkg);;Todos los archivos (*)"])
    if shutil.which("zenity"):
        return _run(["zenity", "--file-selection", "--title=Seleccionar paquete de Styler",
                     f"--filename={start_path.rstrip('/')}/",
                     "--file-filter=Paquetes de Styler | *.stylerpkg",
                     "--file-filter=Todos los archivos | *"])
    return ""


def choose_directory(start: str | Path | None = None) -> str:
    start_path = str(Path(start or Path.home()).expanduser())
    if shutil.which("kdialog"):
        return _run(["kdialog", "--getexistingdirectory", start_path])
    if shutil.which("zenity"):
        return _run(["zenity", "--file-selection", "--directory", "--title=Seleccionar carpeta",
                     f"--filename={start_path.rstrip('/')}/"])
    return ""


def native_dialog_available() -> bool:
    return bool(shutil.which("kdialog") or shutil.which("zenity"))
