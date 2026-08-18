"""Comandos no interactivos de gestores de paquetes.

Módulo neutral: resolvers y ejecutores pueden construir comandos sin depender
de la capa alta ``styler.applications`` y sin crear ciclos de imports.
"""
from __future__ import annotations

from collections.abc import Iterable

APT_NONINTERACTIVE_ENV = (
    "DEBIAN_FRONTEND=noninteractive",
    "DEBCONF_NONINTERACTIVE_SEEN=true",
    "APT_LISTCHANGES_FRONTEND=none",
    "NEEDRESTART_MODE=a",
)


def apt_install_argv(prefix: Iterable[str], *packages: str) -> list[str]:
    """Construye una instalación APT no interactiva y espera el lock de dpkg."""
    return [
        *prefix,
        "env",
        *APT_NONINTERACTIVE_ENV,
        "apt-get",
        "-o", "Dpkg::Use-Pty=0",
        "-o", "DPkg::Lock::Timeout=300",
        "-o", "Dpkg::Options::=--force-confdef",
        "-o", "Dpkg::Options::=--force-confold",
        "-y", "install",
        *packages,
    ]


def apt_update_argv(prefix: Iterable[str]) -> list[str]:
    """Construye una actualización de catálogo APT sin interfaz interactiva."""
    return [
        *prefix,
        "env",
        *APT_NONINTERACTIVE_ENV,
        "apt-get",
        "-o", "Dpkg::Use-Pty=0",
        "-o", "DPkg::Lock::Timeout=300",
        "update",
    ]


def dpkg_configure_argv(prefix: Iterable[str]) -> list[str]:
    """Reanuda de forma segura una configuración de dpkg interrumpida."""
    return [*prefix, "env", *APT_NONINTERACTIVE_ENV, "dpkg", "--configure", "-a"]


def admin_prefix() -> list[str] | None:
    """Prefijo no interactivo para gestores del sistema, o ``None`` si falta elevación."""
    import os
    import shutil

    if os.geteuid() == 0:
        return []
    if shutil.which("sudo"):
        return ["sudo", "-n"]
    if shutil.which("pkexec"):
        return ["pkexec"]
    return None
