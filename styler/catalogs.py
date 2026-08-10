"""
styler.catalogs
===============
El conocimiento sobre el mundo (qué paquete provee KDE en Arch, qué remoto es
flathub, cómo se llama Firefox en openSUSE) **no vive en el código**: vive en
catálogos TOML declarativos.

Añadir COSMIC, NixOS o una distribución nueva no debe obligar a nadie a tocar
Python. Basta con dejar un `.toml` en:

    styler/catalog/            (los que trae Styler)
    <root>/.styler/catalog/    (los de esta biblioteca)
    ~/.config/styler/catalog/  (los tuyos, tienen la última palabra)

Lo que sí sigue en Python es el *protocolo* de cada gestor (`apt-get install`,
`pacman -S`): eso es una capacidad técnica estable, no conocimiento del mundo.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

PACKAGED = Path(__file__).parent / "catalog"
USER_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "styler" / "catalog"


@dataclass(frozen=True)
class Provider:
    family: str
    manager: str
    package: str


@dataclass(frozen=True)
class Capability:
    """Algo que una configuración necesita: un escritorio, un gestor."""

    id: str
    title: str
    identity: str = ""
    verify_executables: tuple[str, ...] = ()
    guide: str = ""
    version_policy: str = "present"      # present | latest
    providers: tuple[Provider, ...] = ()

    def provider_for(self, family: str) -> Optional[Provider]:
        return next((item for item in self.providers if item.family == family), None)

    @property
    def verifiable(self) -> bool:
        """Sin forma de comprobarlo, Styler no puede afirmar que existe."""
        return bool(self.verify_executables)


@dataclass(frozen=True)
class Remote:
    name: str
    manager: str
    url: str
    official: bool = False


@dataclass(frozen=True)
class ApplicationEntry:
    identity: str
    title: str
    names: dict[str, str] = field(default_factory=dict)

    def name_for(self, manager: str) -> str:
        return self.names.get(manager, "")


@dataclass
class Catalog:
    families: dict[str, tuple[str, ...]] = field(default_factory=dict)
    native_managers: dict[str, str] = field(default_factory=dict)
    desktops: dict[str, Capability] = field(default_factory=dict)
    managers: dict[str, Capability] = field(default_factory=dict)
    remotes: dict[str, Remote] = field(default_factory=dict)
    applications: dict[str, ApplicationEntry] = field(default_factory=dict)
    official_apt_hosts: tuple[str, ...] = ()

    # -- consultas -------------------------------------------------------
    def family_for(self, distro_id: str, id_like: str = "") -> str:
        candidates = [distro_id.lower(), *[item.lower() for item in id_like.split()]]
        for candidate in candidates:
            for family, members in self.families.items():
                if candidate == family or candidate in members:
                    return family
        return ""

    def native_manager(self, family: str) -> str:
        return self.native_managers.get(family, "")

    def desktop(self, environment_id: str) -> Optional[Capability]:
        return self.desktops.get(environment_id)

    def manager(self, manager_id: str) -> Optional[Capability]:
        return self.managers.get(manager_id)

    def remote(self, name: str) -> Optional[Remote]:
        return self.remotes.get(name.lower())

    def application(self, identity: str) -> Optional[ApplicationEntry]:
        return self.applications.get(identity)

    def application_by_name(self, manager: str, name: str) -> Optional[ApplicationEntry]:
        for entry in self.applications.values():
            if entry.names.get(manager, "").lower() == name.lower():
                return entry
        return None


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"[styler] catálogo ignorado ({path.name}): {exc}")
        return {}


def _directories(root: str | Path = ".") -> list[Path]:
    return [PACKAGED, Path(root) / ".styler" / "catalog", USER_CONFIG]


def _capabilities(data: dict[str, Any]) -> dict[str, Capability]:
    result: dict[str, Capability] = {}
    for raw in data.get("capability", []):
        providers = tuple(
            Provider(
                family=str(item.get("family", "")),
                manager=str(item.get("manager", "")),
                package=str(item.get("package", "")),
            )
            for item in raw.get("provider", [])
        )
        capability = Capability(
            id=str(raw.get("id", "")),
            title=str(raw.get("title", raw.get("id", ""))),
            identity=str(raw.get("identity", "")),
            verify_executables=tuple(raw.get("verify_executables", [])),
            guide=str(raw.get("guide", "")),
            version_policy=str(raw.get("version_policy", "present")),
            providers=providers,
        )
        if capability.id:
            result[capability.id] = capability
    return result


def load(root: str | Path = ".") -> Catalog:
    """Carga los catálogos. Los del usuario pisan a los de Styler."""
    catalog = Catalog()
    for directory in _directories(root):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.toml")):
            data = _read(path)

            for family, raw in data.get("families", {}).items():
                catalog.families[family] = tuple(raw.get("members", []))
                if raw.get("native_manager"):
                    catalog.native_managers[family] = str(raw["native_manager"])

            stem = path.stem
            if stem == "desktops":
                catalog.desktops.update(_capabilities(data))
            elif stem == "managers":
                catalog.managers.update(_capabilities(data))
            elif "capability" in data and stem not in ("desktops", "managers"):
                # Un catálogo del usuario puede declarar de qué tipo es.
                target = data.get("kind", "desktops")
                (catalog.desktops if target == "desktops" else catalog.managers).update(
                    _capabilities(data)
                )

            for raw in data.get("remote", []):
                remote = Remote(
                    name=str(raw.get("name", "")).lower(),
                    manager=str(raw.get("manager", "flatpak")),
                    url=str(raw.get("url", "")),
                    official=bool(raw.get("official", False)),
                )
                if remote.name and remote.url:
                    catalog.remotes[remote.name] = remote

            for raw in data.get("application", []):
                entry = ApplicationEntry(
                    identity=str(raw.get("identity", "")),
                    title=str(raw.get("title", "")),
                    names={
                        str(key): str(value)
                        for key, value in (raw.get("names", {}) or {}).items()
                    },
                )
                if entry.identity:
                    catalog.applications[entry.identity] = entry

            if data.get("official_hosts"):
                catalog.official_apt_hosts = tuple(
                    str(host).lower() for host in data["official_hosts"]
                )
    return catalog


_CACHE: dict[str, Catalog] = {}


def cached(root: str | Path = ".") -> Catalog:
    key = str(root)
    if key not in _CACHE:
        _CACHE[key] = load(root)
    return _CACHE[key]


def clear_cache() -> None:
    _CACHE.clear()
