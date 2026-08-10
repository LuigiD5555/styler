"""
styler.compat
================
Detecta el entorno real y reporta compatibilidad SIN exagerar.

Regla explícita del proyecto: no se afirma compatibilidad con GNOME,
Cinnamon o XFCE mientras no exista. Un escritorio que no es KDE Plasma
se reporta como «Compatibilidad experimental» o «Todavía no
compatible», nunca como compatible.

Estados posibles (los que la GUI pinta con ✓, ⚠ y ✕):

    ok            → esta parte funciona aquí
    experimental  → puede funcionar, no está probado
    unsupported   → esta parte requiere otro escritorio
    missing       → falta instalar algo (p.ej. el tema Papirus)
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, field

from styler.parts import part_by_id
from styler.runtime.commands import PipeCraftRunner

KDE = "kde"
GTK = "gtk"
ANY = "any"


@dataclass
class Environment:
    distro: str = ""
    desktop: str = ""          # KDE | GNOME | XFCE | Cinnamon | desconocido
    desktop_version: str = ""
    session: str = ""          # x11 | wayland | desconocido
    available_commands: set[str] = field(default_factory=set)

    def is_kde(self) -> bool:
        return "kde" in self.desktop.lower() or "plasma" in self.desktop.lower()


def detect_environment() -> Environment:
    distro = ""
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    distro = line.split("=", 1)[1].strip().strip('"')
    except FileNotFoundError:
        distro = platform.platform()

    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("DESKTOP_SESSION", "")
    session = os.environ.get("XDG_SESSION_TYPE", "")

    desktop_version = ""
    if shutil.which("plasmashell"):
        result = PipeCraftRunner(timeout=5).run(["plasmashell", "--version"], timeout=5)
        if result.returncode == 0:
            desktop_version = result.stdout.strip()

    available = {
        command
        for command in ("plasmashell", "kwriteconfig5", "kwriteconfig6", "systemctl", "flatpak")
        if shutil.which(command)
    }

    return Environment(
        distro=distro,
        desktop=desktop or "desconocido",
        desktop_version=desktop_version,
        session=session or "desconocido",
        available_commands=available,
    )


@dataclass
class PartCompatibility:
    part_id: str
    title: str
    status: str        # ok | experimental | unsupported | missing
    message: str       # texto visible

    def symbol(self) -> str:
        return {"ok": "✓", "experimental": "⚠", "missing": "⚠", "unsupported": "✕"}.get(self.status, "⚠")


def compatibility_for(layers, environment: Environment) -> list[dict]:
    """Evalúa cada capa contra el entorno actual. Devuelve dicts listos
    para pintar en la interfaz."""
    results: list[PartCompatibility] = []
    seen: set[str] = set()

    for layer in layers:
        if layer.part_id in seen:
            continue
        seen.add(layer.part_id)
        definition = part_by_id(layer.part_id)
        required_desktop = definition.desktop if definition else ANY
        title = definition.title if definition else layer.part_id

        if required_desktop == ANY:
            results.append(PartCompatibility(layer.part_id, title, "ok", f"{title}: compatible"))
        elif required_desktop == KDE and environment.is_kde():
            results.append(PartCompatibility(layer.part_id, title, "ok", f"{title}: compatible"))
        elif required_desktop == KDE and environment.desktop == "desconocido":
            results.append(
                PartCompatibility(
                    layer.part_id, title, "experimental",
                    f"{title}: compatibilidad experimental (no se pudo confirmar tu escritorio)",
                )
            )
        elif required_desktop == KDE:
            results.append(
                PartCompatibility(
                    layer.part_id, title, "unsupported",
                    f"{title}: esta parte requiere KDE Plasma",
                )
            )
        else:
            results.append(
                PartCompatibility(
                    layer.part_id, title, "experimental",
                    f"{title}: compatibilidad experimental",
                )
            )

    return [
        {
            "part_id": item.part_id,
            "titulo": item.title,
            "estado": item.status,
            "simbolo": item.symbol(),
            "mensaje": item.message,
        }
        for item in results
    ]
