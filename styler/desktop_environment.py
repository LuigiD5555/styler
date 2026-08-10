"""Detección y metadatos reproducibles de entornos de escritorio.

Styler no guarda el escritorio ni descarga temas como archivos del usuario.
Registra que el entorno existía, qué paquete lo proporcionaba y cuál es la
ruta oficial recomendada para volver a instalarlo.
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Iterable

from styler.models import DesktopEnvironmentRecord, Package
from styler.runtime.commands import PipeCraftRunner

KDE_PROJECT_URL = "https://kde.org/plasma-desktop/"
KDE_INSTALL_URL = "https://kde.org/distributions/"

# Se prefieren metapaquetes porque reproducen mejor un escritorio completo.
KDE_PROVIDER_PREFERENCE: dict[str, tuple[str, ...]] = {
    "apt": (
        "kde-plasma-desktop",
        "kde-standard",
        "kubuntu-desktop",
        "plasma-desktop",
        "plasma-workspace",
    ),
    "pacman": ("plasma-meta", "plasma-desktop", "plasma-workspace"),
    "rpm": ("plasma-desktop", "plasma-workspace"),
    "zypper": ("patterns-kde-kde_plasma", "plasma6-desktop", "plasma-desktop"),
}

KDE_PACKAGE_MARKERS = {
    name for names in KDE_PROVIDER_PREFERENCE.values() for name in names
}


def _desktop_tokens() -> set[str]:
    values = (
        os.environ.get("XDG_CURRENT_DESKTOP", ""),
        os.environ.get("DESKTOP_SESSION", ""),
        os.environ.get("XDG_SESSION_DESKTOP", ""),
    )
    return {
        token.strip().lower()
        for value in values
        for token in re.split(r"[:;,\s]+", value)
        if token.strip()
    }


def _best_kde_package(packages: Iterable[Package]) -> Package | None:
    by_key = {
        (package.manager.lower(), package.name.lower()): package
        for package in packages
    }
    for manager, names in KDE_PROVIDER_PREFERENCE.items():
        for name in names:
            match = by_key.get((manager, name))
            if match is not None:
                return match
    return None


def _plasma_version() -> str:
    executable = shutil.which("plasmashell")
    if not executable:
        return ""
    result = PipeCraftRunner(timeout=10).run([executable, "--version"], timeout=10)
    text = (result.stdout or result.stderr).strip()
    match = re.search(r"(\d+(?:\.\d+){1,3})", text)
    return match.group(1) if match else ""


def detect_desktop_environments(
    packages: Iterable[Package] = (),
) -> list[DesktopEnvironmentRecord]:
    """Detecta Plasma aun cuando la captura se ejecute fuera de su sesión."""
    packages = list(packages)
    tokens = _desktop_tokens()
    provider = _best_kde_package(packages)
    evidence: list[str] = []

    if any("kde" in token or "plasma" in token for token in tokens):
        evidence.append("sesión gráfica")
    if provider is not None:
        evidence.append(f"paquete {provider.manager}:{provider.name}")
    if shutil.which("plasmashell"):
        evidence.append("ejecutable plasmashell")

    if not evidence:
        return []

    version = _plasma_version() or (provider.version if provider else "")
    return [
        DesktopEnvironmentRecord(
            environment_id="kde-plasma",
            name="KDE Plasma",
            version=version,
            package_manager=provider.manager if provider else "",
            package_name=provider.name if provider else "",
            official_project_url=KDE_PROJECT_URL,
            official_install_url=KDE_INSTALL_URL,
            detected_by=evidence,
        )
    ]


def kde_provider_package(packages: Iterable[Package]) -> Package | None:
    """API pública usada al asociar Plasma con paneles y atajos."""
    return _best_kde_package(packages)
