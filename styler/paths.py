"""Rutas de datos y configuración de Styler.

La TUI no debe depender del directorio desde el que se ejecutó el comando.
Se siguen las variables XDG y se crea una biblioteca estable en el HOME.
"""
from __future__ import annotations

import os
from pathlib import Path


def default_library_root() -> Path:
    """Devuelve la biblioteca persistente de Styler.

    XDG_DATA_HOME tiene prioridad. Cuando no está definido se usa la ubicación
    estándar ``~/.local/share/styler``.
    """
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (base / "styler").expanduser()


def ensure_library_root(path: str | Path | None = None) -> Path:
    root = Path(path).expanduser() if path else default_library_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def default_export_directory() -> Path:
    """Directorio amigable para perfiles exportados."""
    xdg_download = os.environ.get("XDG_DOWNLOAD_DIR", "")
    if xdg_download:
        candidate = Path(xdg_download.replace("$HOME", str(Path.home()))).expanduser()
        if candidate.is_dir():
            return candidate
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()
