"""Clasifica inventario observado para el Constructor de cambios."""
from __future__ import annotations

from styler.provenance.models import (
    AppCategory,
    ApplicationRecord,
    InstallReason,
    OriginKind,
)

_SNAP_INFRASTRUCTURE = frozenset({"core", "core18", "core20", "core22", "core24", "snapd", "bare"})
_DEV_MANAGERS = frozenset({"brew", "nix", "pipx", "cargo", "npm", "language-tool"})


def classify(record: ApplicationRecord) -> AppCategory:
    """Clasifica sin presentar dependencias o infraestructura como aplicaciones."""
    if record.category != AppCategory.UNKNOWN:
        return record.category
    if record.manager == "snap" and record.name in _SNAP_INFRASTRUCTURE:
        return AppCategory.SYSTEM_BASE
    if record.manager in _DEV_MANAGERS:
        return AppCategory.DEV_TOOL
    if record.origin.kind == OriginKind.CONTAINER:
        return AppCategory.ISOLATED_ENV
    if record.install_reason == InstallReason.DEPENDENCY:
        return AppCategory.SYSTEM_BASE
    if record.manager in {"apt", "flatpak", "snap", "pacman", "aur", "rpm", "zypper", "appimage"}:
        return AppCategory.DESKTOP_APP
    return AppCategory.UNKNOWN


def is_user_choice(record: ApplicationRecord) -> bool:
    """Devuelve ``False`` para dependencias e infraestructura automática."""
    if classify(record) == AppCategory.SYSTEM_BASE:
        return False
    return record.install_reason != InstallReason.DEPENDENCY


def can_generate_install(record: ApplicationRecord) -> tuple[bool, str]:
    """Evalúa si el Constructor puede crear una operación ejecutable y honesta."""
    if record.manager == "appimage":
        if record.integrity.artifact_available and record.integrity.artifact_path:
            return True, "El AppImage se incluirá dentro del paquete."
        return False, "El archivo AppImage ya no está disponible para incorporarlo."
    if record.manager not in {"apt", "flatpak", "pacman", "aur", "rpm", "zypper", "snap"}:
        return False, f"El gestor '{record.manager or 'desconocido'}' no tiene una operación ejecutable."
    if not record.reproducible_today:
        return False, "No hay un repositorio confirmado ni un artefacto que permita reconstruir la instalación."
    if not (record.origin.remote_name or record.origin.remote_url):
        return False, "No se conoce el origen desde el que otro equipo podría instalar la aplicación."
    return True, "Styler puede generar una instalación declarativa desde el origen detectado."
