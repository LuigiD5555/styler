"""Inferencia conservadora y comprobación de dependencias.

No se adivina una dependencia por cualquier parecido. Solo se asocia cuando
hay una relación conocida con una aplicación o cuando el nombre de un recurso
instalado coincide claramente con un paquete observado.
"""
from __future__ import annotations

import re
import shutil
from pathlib import PurePosixPath

from styler.desktop_environment import kde_provider_package
from styler.models import FileEntry, Package
from styler.runtime.commands import PipeCraftRunner

KNOWN_APPLICATION_PACKAGES: dict[str, tuple[str, ...]] = {
    "konsole": ("konsole", "org.kde.konsole"),
    "dolphin": ("dolphin", "org.kde.dolphin"),
    "gimp": ("gimp", "org.gimp.GIMP"),
}


KDE_PARTS = {"tema-colores", "paneles", "atajos", "konsole", "dolphin"}

RESOURCE_PREFIXES = (
    "${HOME}/.icons/",
    "${HOME}/.local/share/icons/",
    "${HOME}/.themes/",
    "${HOME}/.local/share/themes/",
    "${HOME}/.local/share/plasma/desktoptheme/",
    "${HOME}/.local/share/plasma/look-and-feel/",
    "${HOME}/.local/share/color-schemes/",
    "${HOME}/.local/share/cursors/",
)


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _resource_names(entries: list[FileEntry]) -> set[str]:
    names: set[str] = set()
    for entry in entries:
        for prefix in RESOURCE_PREFIXES:
            if entry.path.startswith(prefix):
                relative = entry.path[len(prefix):]
                first = PurePosixPath(relative).parts[0] if relative else ""
                token = _normalized(first.rsplit(".", 1)[0])
                if len(token) >= 4 and token not in {"default", "breeze", "share"}:
                    names.add(token)
    return names


def infer_packages(part_id: str, entries: list[FileEntry], installed: list[Package]) -> list[Package]:
    """Devuelve únicamente paquetes observados que se pueden relacionar."""
    selected: dict[tuple[str, str], Package] = {}
    known = {_normalized(name) for name in KNOWN_APPLICATION_PACKAGES.get(part_id, ())}
    resources = _resource_names(entries)

    for package in installed:
        package_token = _normalized(package.name)
        known_match = package_token in known
        resource_match = any(resource in package_token for resource in resources)
        if known_match or resource_match:
            selected[(package.manager, package.name)] = package

    if part_id in KDE_PARTS:
        provider = kde_provider_package(installed)
        if provider is not None:
            selected[(provider.manager, provider.name)] = provider
    return sorted(selected.values(), key=lambda item: (item.manager, item.name.lower()))


def is_installed(package: Package) -> bool | None:
    """True/False cuando el gestor está disponible; None si no se puede saber."""
    commands: dict[str, tuple[str, list[str], int]] = {
        "apt": ("dpkg-query", ["dpkg-query", "-W", "-f=${Status}", package.name], 10),
        "flatpak": ("flatpak", ["flatpak", "info", package.name], 15),
        "pacman": ("pacman", ["pacman", "-Q", package.name], 10),
        "rpm": ("rpm", ["rpm", "-q", package.name], 10),
        "zypper": ("rpm", ["rpm", "-q", package.name], 10),
        "snap": ("snap", ["snap", "list", package.name], 15),
    }
    entry = commands.get(package.manager)
    if entry is None:
        return None
    program, argv, timeout = entry
    if not shutil.which(program):
        return None
    result = PipeCraftRunner(timeout=timeout).run(argv, timeout=timeout)
    if package.manager == "apt":
        return result.returncode == 0 and "install ok installed" in result.stdout
    return result.returncode == 0

