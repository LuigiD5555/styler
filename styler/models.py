"""Estructuras de datos centrales y serialización estable de Styler."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from styler.applications import AppSpec


@dataclass(frozen=True)
class ObjectRef:
    """Referencia portable a contenido; nunca contiene una ruta local."""
    checksum: str
    size: int = 0


@dataclass
class Package:
    manager: str
    name: str
    version: str = ""
    architecture: str = ""


@dataclass
class DesktopEnvironmentRecord:
    """Entorno base observado; no contiene archivos de temas ni binarios."""

    environment_id: str
    name: str
    version: str = ""
    package_manager: str = ""
    package_name: str = ""
    official_project_url: str = ""
    official_install_url: str = ""
    detected_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment_id": self.environment_id,
            "name": self.name,
            "version": self.version,
            "package_manager": self.package_manager,
            "package_name": self.package_name,
            "official_project_url": self.official_project_url,
            "official_install_url": self.official_install_url,
            "detected_by": list(self.detected_by),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "DesktopEnvironmentRecord":
        return DesktopEnvironmentRecord(
            environment_id=str(data.get("environment_id", "")),
            name=str(data.get("name", "")),
            version=str(data.get("version", "")),
            package_manager=str(data.get("package_manager", "")),
            package_name=str(data.get("package_name", "")),
            official_project_url=str(data.get("official_project_url", "")),
            official_install_url=str(data.get("official_install_url", "")),
            detected_by=[str(item) for item in data.get("detected_by", [])],
        )

    def package(self) -> Package | None:
        if not self.package_manager or not self.package_name:
            return None
        return Package(
            manager=self.package_manager,
            name=self.package_name,
            version=self.version,
        )


@dataclass
class FileEntry:
    path: str
    checksum: str
    size: int = 0
    mode: str = ""
    owner_hint: str = ""
    # Compatibilidad de lectura con v0.3. No se vuelve a serializar ni se usa
    # para localizar contenido; el ObjectStore resuelve siempre por checksum.
    object_path: str = ""

    @property
    def object_ref(self) -> ObjectRef:
        return ObjectRef(checksum=self.checksum, size=self.size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "checksum": self.checksum,
            "size": self.size,
            "mode": self.mode,
            "owner_hint": self.owner_hint,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "FileEntry":
        return FileEntry(
            path=data["path"],
            checksum=data.get("checksum", ""),
            size=int(data.get("size", 0) or 0),
            mode=str(data.get("mode", "") or ""),
            owner_hint=str(data.get("owner_hint", "") or ""),
            object_path=str(data.get("object_path", "") or ""),
        )


@dataclass
class ServiceEntry:
    name: str
    scope: str = "user"
    enabled: bool = True


@dataclass
class State:
    state_id: str
    label: str
    captured_at: float = field(default_factory=time.time)
    distro: str = ""
    base: str = ""
    desktops: list[str] = field(default_factory=list)
    desktop_environments: list[DesktopEnvironmentRecord] = field(default_factory=list)
    packages: list[Package] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)
    services: list[ServiceEntry] = field(default_factory=list)
    # Aplicaciones que la persona instaló a propósito (no toda la lista de dpkg).
    # Es lo que Styler sabe volver a instalar; ver styler/applications.py.
    applications: list[AppSpec] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_id": self.state_id,
            "label": self.label,
            "captured_at": self.captured_at,
            "distro": self.distro,
            "base": self.base,
            "desktops": list(self.desktops),
            "desktop_environments": [item.to_dict() for item in self.desktop_environments],
            "packages": [asdict(package) for package in self.packages],
            "files": [entry.to_dict() for entry in self.files],
            "services": [asdict(service) for service in self.services],
            "applications": [app.to_dict() for app in self.applications],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "State":
        return State(
            state_id=d["state_id"],
            label=d.get("label", ""),
            captured_at=d.get("captured_at", time.time()),
            distro=d.get("distro", ""),
            base=d.get("base", ""),
            desktops=d.get("desktops", []),
            desktop_environments=[
                DesktopEnvironmentRecord.from_dict(item)
                for item in d.get("desktop_environments", [])
            ],
            packages=[Package(**p) for p in d.get("packages", [])],
            files=[FileEntry.from_dict(f) for f in d.get("files", [])],
            services=[ServiceEntry(**s) for s in d.get("services", [])],
            applications=[AppSpec.from_dict(a) for a in d.get("applications", [])],
        )


class ChangeKind(str, Enum):
    PACKAGE_ADDED = "package_added"
    PACKAGE_REMOVED = "package_removed"
    FILE_ADDED = "file_added"
    FILE_MODIFIED = "file_modified"
    FILE_REMOVED = "file_removed"
    SERVICE_ADDED = "service_added"
    SERVICE_REMOVED = "service_removed"


@dataclass
class RawChange:
    kind: ChangeKind
    subject: str
    detail: dict[str, Any] = field(default_factory=dict)


class Decision(str, Enum):
    INCLUDE = "include"
    PENDING = "pending"
    PERSONAL = "personal"
    IGNORED = "ignored"
    UNDECIDED = "undecided"


@dataclass
class Component:
    component_id: str
    title: str
    category: str
    depends_on: list[str] = field(default_factory=list)
    packages: list[Package] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)
    services: list[ServiceEntry] = field(default_factory=list)
    source: Optional[dict[str, Any]] = None
    decision: Decision = Decision.UNDECIDED
    human_summary: str = ""
    # Modelo semántico. `depends_on` conserva el grafo resuelto por ID;
    # estas listas describen relaciones estables entre capacidades.
    component_type: str = "generic"
    provides: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    replaces: list[str] = field(default_factory=list)
    provider_variants: dict[str, dict[str, str]] = field(default_factory=dict)
    verification: list[dict[str, Any]] = field(default_factory=list)
    selected_provider: str = ""
    selected_variant: str = ""
    target_root: str = ""
    baseline_role: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "title": self.title,
            "category": self.category,
            "depends_on": list(self.depends_on),
            "packages": [asdict(package) for package in self.packages],
            "files": [entry.to_dict() for entry in self.files],
            "services": [asdict(service) for service in self.services],
            "source": self.source,
            "decision": self.decision.value,
            "human_summary": self.human_summary,
            "component_type": self.component_type,
            "provides": list(self.provides),
            "requires": list(self.requires),
            "optional": list(self.optional),
            "conflicts": list(self.conflicts),
            "replaces": list(self.replaces),
            "provider_variants": dict(self.provider_variants),
            "verification": list(self.verification),
            "selected_provider": self.selected_provider,
            "selected_variant": self.selected_variant,
            "target_root": self.target_root,
            "baseline_role": self.baseline_role,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Component":
        return Component(
            component_id=raw["component_id"],
            title=raw.get("title", raw["component_id"]),
            category=raw.get("category", "sin_clasificar"),
            depends_on=list(raw.get("depends_on", [])),
            packages=[Package(**package) for package in raw.get("packages", [])],
            files=[FileEntry.from_dict(entry) for entry in raw.get("files", [])],
            services=[ServiceEntry(**service) for service in raw.get("services", [])],
            source=raw.get("source"),
            decision=Decision(raw.get("decision", "undecided")),
            human_summary=raw.get("human_summary", ""),
            component_type=raw.get("component_type", "generic"),
            provides=list(raw.get("provides", [])),
            requires=list(raw.get("requires", [])),
            optional=list(raw.get("optional", [])),
            conflicts=list(raw.get("conflicts", [])),
            replaces=list(raw.get("replaces", [])),
            provider_variants=dict(raw.get("provider_variants", {})),
            verification=list(raw.get("verification", [])),
            selected_provider=raw.get("selected_provider", ""),
            selected_variant=raw.get("selected_variant", ""),
            target_root=raw.get("target_root", ""),
            baseline_role=raw.get("baseline_role", "unknown"),
        )


@dataclass
class Changeset:
    changeset_id: str
    base_state: str
    target_state: str
    components: list[Component] = field(default_factory=list)

    def included(self) -> list[Component]:
        return [c for c in self.components if c.decision == Decision.INCLUDE]
