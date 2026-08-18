"""Información estable del sistema sin depender de captura ni procedencia."""
from __future__ import annotations

import platform


def detect_distro() -> tuple[str, str]:
    """Lee /etc/os-release cuando existe; si no, usa platform como
    respaldo (esto corre en cualquier host, no solo en Mint 22.3)."""
    info = {}
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    info[k] = v.strip('"')
    except FileNotFoundError:
        pass
    distro = info.get("PRETTY_NAME", platform.platform())
    base = info.get("ID_LIKE", info.get("ID", ""))
    return distro, base
