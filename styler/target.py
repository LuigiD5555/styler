"""
styler.target
=============
El equipo **destino** decide cómo se cumple un requisito.

El perfil guarda una *intención* («este escritorio usaba KDE Plasma»), no un
paquete concreto. La traducción intención → paquete de esta distribución no está
escrita en Python: sale de los catálogos TOML (`styler/catalogs.py`).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from styler import catalogs


@dataclass(frozen=True)
class Target:
    """La distribución donde Styler está restaurando ahora mismo."""

    distro_id: str = ""
    version: str = ""
    family: str = ""          # ubuntu | debian | arch | fedora | suse | ""
    pretty_name: str = ""
    id_like: str = ""
    version_codename: str = ""
    ubuntu_codename: str = ""
    root: str = "."

    @property
    def native_manager(self) -> str:
        return catalogs.cached(self.root).native_manager(self.family)

    @property
    def known(self) -> bool:
        return bool(self.family)

    def to_dict(self) -> dict:
        return {
            "distro_id": self.distro_id,
            "version": self.version,
            "family": self.family,
            "pretty_name": self.pretty_name,
            "id_like": self.id_like,
            "version_codename": self.version_codename,
            "ubuntu_codename": self.ubuntu_codename,
            "native_manager": self.native_manager,
        }


def detect_target(os_release: str | Path = "/etc/os-release", root: str = ".") -> Target:
    info: dict[str, str] = {}
    try:
        for line in Path(os_release).read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, value = line.split("=", 1)
                info[key.strip()] = value.strip().strip('"')
    except OSError:
        return Target(root=root)
    distro_id = info.get("ID", "")
    return Target(
        distro_id=distro_id,
        version=info.get("VERSION_ID", ""),
        family=catalogs.cached(root).family_for(distro_id, info.get("ID_LIKE", "")),
        pretty_name=info.get("PRETTY_NAME", distro_id),
        id_like=info.get("ID_LIKE", ""),
        version_codename=info.get("VERSION_CODENAME", ""),
        ubuntu_codename=info.get("UBUNTU_CODENAME", ""),
        root=root,
    )


# --------------------------------------------------------------------------- #
# Escritorios y gestores (desde el catálogo, no desde tablas en el código)
# --------------------------------------------------------------------------- #

def desktop_capability(environment_id: str, root: str = ".") -> Optional[catalogs.Capability]:
    return catalogs.cached(root).desktop(environment_id)


def manager_capability(manager: str, root: str = ".") -> Optional[catalogs.Capability]:
    return catalogs.cached(root).manager(manager)


def resolve_desktop(environment_id: str, target: Target) -> Optional[tuple[str, str]]:
    """(gestor, paquete) del escritorio en ESTA distribución. None si no se sabe."""
    capability = desktop_capability(environment_id, target.root)
    if capability is None or not target.family:
        return None
    provider = capability.provider_for(target.family)
    return (provider.manager, provider.package) if provider else None


def resolve_manager(manager: str, target: Target) -> Optional[tuple[str, str]]:
    capability = manager_capability(manager, target.root)
    if capability is None or not target.family:
        return None
    provider = capability.provider_for(target.family)
    return (provider.manager, provider.package) if provider else None


def desktop_binaries(environment_id: str, root: str = ".") -> tuple[str, ...]:
    capability = desktop_capability(environment_id, root)
    return capability.verify_executables if capability else ()


def manager_binary(manager: str, root: str = ".") -> str:
    capability = manager_capability(manager, root)
    if capability and capability.verify_executables:
        return capability.verify_executables[0]
    from styler.resolvers import resolver_for

    resolver = resolver_for(manager)
    return resolver.program if resolver else manager


# --------------------------------------------------------------------------- #
# Remotos de Flatpak
# --------------------------------------------------------------------------- #

def remote_add_argv(remote: str, root: str = ".") -> Optional[list[str]]:
    """Solo se añaden remotos declarados como oficiales en el catálogo."""
    entry = catalogs.cached(root).remote(remote)
    if entry is None or not entry.official:
        return None
    return ["flatpak", "remote-add", "--if-not-exists", entry.name, entry.url]


def configured_flatpak_remotes(runner) -> set[str]:
    if not runner.available("flatpak"):
        return set()
    result = runner.run(["flatpak", "remotes", "--columns=name"], timeout=30)
    if result.returncode != 0:
        return set()
    return {line.strip().lower() for line in result.stdout.splitlines() if line.strip()}


# --------------------------------------------------------------------------- #
# Repositorios de APT
# --------------------------------------------------------------------------- #

def _host_of(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0].lower().split("@")[-1]


def _matches_host(host: str, known: str) -> bool:
    """`evilarchive.ubuntu.com` NO pertenece a `archive.ubuntu.com`."""
    return host == known or host.endswith("." + known)


def official_apt_hosts(root: str = ".") -> tuple[str, ...]:
    return catalogs.cached(root).official_apt_hosts


def is_third_party_apt(remote_url: str, root: str = ".") -> bool:
    if not remote_url:
        return False
    host = _host_of(remote_url)
    return not any(_matches_host(host, known) for known in official_apt_hosts(root))


def _active_source_lines(base: str | Path = "/etc/apt") -> list[str]:
    """Solo líneas activas: una fuente comentada NO está configurada."""
    directory = Path(base)
    candidates: list[Path] = []
    if (directory / "sources.list").is_file():
        candidates.append(directory / "sources.list")
    listd = directory / "sources.list.d"
    if listd.is_dir():
        candidates.extend(sorted(listd.glob("*.list")))
        candidates.extend(sorted(listd.glob("*.sources")))

    lines: list[str] = []
    for path in candidates:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if path.suffix == ".sources":
            # deb822: «Enabled: no» desactiva el bloque entero.
            for block in text.split("\n\n"):
                low = block.lower()
                if "enabled: no" in low or "enabled: false" in low:
                    continue
                lines.extend(
                    line.strip()
                    for line in block.splitlines()
                    if line.strip().lower().startswith("uris:")
                )
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)
    return lines


def apt_repository_configured(remote_url: str, base: str | Path = "/etc/apt") -> bool:
    if not remote_url:
        return True
    host = _host_of(remote_url)
    for line in _active_source_lines(base):
        for token in line.split():
            if "://" not in token:
                continue
            if _matches_host(_host_of(token), host) or _host_of(token) == host:
                return True
    return False
