"""Modelos tipados del catálogo declarativo de componentes.

Un ``ComponentDefinition`` describe qué es un componente (GIMP, PhotoGIMP,
KDE Plasma...), qué necesita, qué ofrece y cómo se instala/verifica/revierte.
El scheduler nunca ve estos modelos directamente: los consume a través del
compilador hacia DAG (fase siguiente), que los traduce a nodos runtime
genéricos. Aquí solo vive la descripción declarativa y su validez estructural
mínima (tipos, presencia de campos); la validación semántica (capacidades sin
proveedor, ciclos, rollback irreal) vive en ``validator.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SUPPORTED_SCHEMA_VERSIONS: tuple[int, ...] = (1,)

# Los tres orígenes posibles de una definición, en orden de prioridad
# (el usuario pisa al proyecto, que pisa al catálogo oficial empaquetado).
SOURCE_OFFICIAL = "official"
SOURCE_PROJECT = "project"
SOURCE_PACKAGE = "package"
SOURCE_USER = "user"
SOURCE_PRIORITY: dict[str, int] = {
    SOURCE_USER: 3,
    SOURCE_PACKAGE: 2,
    SOURCE_PROJECT: 1,
    SOURCE_OFFICIAL: 0,
}

VALID_CRITICALITY = ("required", "optional")
VALID_ROLLBACK_LEVELS = ("full", "best_effort", "none")
VALID_COMPATIBILITY_VALUES = (
    "supported",
    "unsupported",
    "wayland_limited",
    "inherits_dependency",
)


@dataclass(frozen=True)
class ComponentSource:
    """De dónde vino una definición: archivo, nivel y prioridad relativa."""

    path: str
    level: str  # official | project | package | user

    @property
    def priority(self) -> int:
        return SOURCE_PRIORITY.get(self.level, 0)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "level": self.level, "priority": self.priority}


@dataclass(frozen=True)
class ResourceDefinition:
    """Recursos exclusivos (serializados) y compartidos que usa un componente.

    ``paths`` mapea un recurso a su ruta real en el sistema de archivos
    (``"user-config:kde" = "${HOME}/.config"``). Sin este mapeo, un paso de
    respaldo o de aplicación de configuración no tiene destino que usar y
    debe fallar explícito en vez de inventar una ruta.
    """

    exclusive: tuple[str, ...] = ()
    shared: tuple[str, ...] = ()
    paths: dict[str, str] = field(default_factory=dict)

    def path_for(self, resource: str) -> str:
        return self.paths.get(resource, "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "exclusive": list(self.exclusive),
            "shared": list(self.shared),
            "paths": dict(self.paths),
        }


@dataclass(frozen=True)
class ProviderDefinition:
    """Una forma concreta de satisfacer un componente (APT, Flatpak, archivo...).

    ``config_root`` es la ruta de configuración de usuario que ESE proveedor
    concreto usa: GIMP por APT guarda en ``~/.config/GIMP``, pero por Flatpak
    guarda en ``~/.var/app/org.gimp.GIMP/config/GIMP``. Un overlay que escribe
    sobre GIMP necesita saber cuál de las dos, así que la ruta depende del
    proveedor elegido, no del componente.
    """

    id: str
    type: str  # apt | flatpak | snap | archive | ...
    families: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()
    application_id: str = ""
    source: str = ""
    config_root: str = ""
    priority: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "families": list(self.families),
            "packages": list(self.packages),
            "application_id": self.application_id,
            "source": self.source,
            "config_root": self.config_root,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class InstallDefinition:
    """Reservado para políticas de instalación explícitas por componente.

    En esta fase la instalación se deriva de ``providers``; este modelo
    existe para no romper el contrato cuando un componente necesite pasos
    adicionales (por ejemplo, orden de post-instalación) sin renegociar el
    esquema TOML de nuevo.
    """

    pre_steps: tuple[str, ...] = ()
    post_steps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"pre_steps": list(self.pre_steps), "post_steps": list(self.post_steps)}


@dataclass(frozen=True)
class VerificationDefinition:
    """Cómo se comprueba que un componente quedó realmente instalado."""

    checks: tuple[str, ...] = ()

    @property
    def verifiable(self) -> bool:
        return bool(self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {"checks": list(self.checks)}


@dataclass(frozen=True)
class RollbackDefinition:
    """Qué tan reversible es un componente y con qué estrategia."""

    level: str = "none"  # full | best_effort | none
    strategy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "strategy": self.strategy}


@dataclass(frozen=True)
class CompatibilityDefinition:
    """Comportamiento declarado en Wayland, XWayland y X11."""

    wayland: str = "supported"
    xwayland: str = "supported"
    x11: str = "supported"

    def to_dict(self) -> dict[str, Any]:
        return {"wayland": self.wayland, "xwayland": self.xwayland, "x11": self.x11}


@dataclass
class ComponentDefinition:
    """Definición declarativa completa de un componente del catálogo."""

    schema_version: int
    id: str
    name: str
    kind: str
    description: str = ""

    requires: tuple[str, ...] = ()
    optional_requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    providers: tuple[ProviderDefinition, ...] = ()
    resources: ResourceDefinition = field(default_factory=ResourceDefinition)
    install: InstallDefinition = field(default_factory=InstallDefinition)
    verification: VerificationDefinition = field(default_factory=VerificationDefinition)
    rollback: RollbackDefinition = field(default_factory=RollbackDefinition)
    compatibility: CompatibilityDefinition = field(default_factory=CompatibilityDefinition)

    criticality: str = "required"
    messages: dict[str, str] = field(default_factory=dict)

    # ID de capacidad que este componente expone al catálogo declarativo
    # (``component_graph.CAPABILITY_PROVIDERS``): p. ej. "application.gimp".
    # Permite que el catálogo TOML sea la única fuente de verdad y que el
    # runtime de componentes consuma estos datos desde el catálogo.
    capability_alias: str = ""

    source: ComponentSource | None = None

    def config_root_for(self, provider_id: str) -> str:
        for provider in self.providers:
            if provider.id == provider_id:
                return provider.config_root
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "requires": list(self.requires),
            "optional_requires": list(self.optional_requires),
            "provides": list(self.provides),
            "conflicts": list(self.conflicts),
            "providers": [provider.to_dict() for provider in self.providers],
            "resources": self.resources.to_dict(),
            "install": self.install.to_dict(),
            "verification": self.verification.to_dict(),
            "rollback": self.rollback.to_dict(),
            "compatibility": self.compatibility.to_dict(),
            "criticality": self.criticality,
            "messages": dict(self.messages),
            "capability_alias": self.capability_alias,
            "source": self.source.to_dict() if self.source else None,
        }
