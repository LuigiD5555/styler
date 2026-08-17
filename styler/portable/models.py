"""Modelo canónico del único formato portable de Styler: ``.stylerpkg``."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from styler.automation.specs import ActionRegistry, ActionSpec, SpecError
from styler.portable.security import audit_execution_surface, audit_paths

PACKAGE_SCHEMA = "styler.package/2"
ACTION_SCHEMA = "styler.action/1"
GRAPH_SCHEMA = "styler.graph/1"
PACKAGE_SUFFIX = ".stylerpkg"
SUPPORTED_ARTIFACT_KINDS = frozenset({"baseline", "recipe", "action", "graph", "component", "asset"})
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}(?:[-+][a-zA-Z0-9._-]+)?$")


class PortablePackageError(SpecError):
    """El paquete no puede importarse de forma segura o consistente."""


class PackageType(str, Enum):
    CHANGE = "change"
    BASELINE = "baseline"

    @property
    def human(self) -> str:
        return "Cambio" if self is PackageType.CHANGE else "Línea base"


def validate_identifier(value: str, label: str = "identificador") -> str:
    text = str(value).strip()
    if not _ID_RE.match(text):
        raise PortablePackageError(
            f"{label.capitalize()} inválido: '{text}'. Usa minúsculas, dígitos, punto, guion o guion bajo."
        )
    return text


def normalize_identifier(value: str, *, fallback: str = "package") -> str:
    """Convierte texto humano en un identificador portable seguro.

    Se usa únicamente al *crear* contenido desde Styler. Los paquetes externos
    siguen pasando por :func:`validate_identifier` sin correcciones silenciosas,
    de modo que una entrada importada nunca cambia de identidad durante la
    validación.
    """
    raw = str(value or "").strip()
    ascii_text = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    text = ascii_text.lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"[-_.]{2,}", "-", text).strip("-._")
    if not text:
        fallback_text = str(fallback or "package").strip().lower()
        fallback_text = re.sub(r"[^a-z0-9._-]+", "-", fallback_text).strip("-._")
        text = fallback_text or "package"
    return validate_identifier(text[:128], "identificador")


@dataclass(frozen=True)
class ArtifactEntry:
    kind: str
    artifact_id: str
    path: str
    title: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in SUPPORTED_ARTIFACT_KINDS:
            raise PortablePackageError(f"Tipo de artefacto no soportado: '{self.kind}'.")
        validate_identifier(self.artifact_id, "identificador de artefacto")
        if not self.path or self.path.startswith("/") or ".." in self.path.split("/"):
            raise PortablePackageError(f"Ruta de artefacto insegura: '{self.path}'.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "artifact_id": self.artifact_id,
            "path": self.path,
            "title": self.title,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactEntry":
        if not isinstance(data, Mapping):
            raise PortablePackageError("Cada artefacto debe ser un objeto.")
        return cls(
            kind=str(data.get("kind", "")),
            artifact_id=str(data.get("artifact_id", data.get("id", ""))),
            path=str(data.get("path", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
        )


@dataclass(frozen=True)
class PackageManifest:
    package_id: str
    name: str
    version: str
    package_type: PackageType
    artifacts: tuple[ArtifactEntry, ...]
    description: str = ""
    author: str = ""
    requires_styler: str = ">=0.11.0"
    requires_capabilities: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = PACKAGE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PACKAGE_SCHEMA:
            raise PortablePackageError(
                f"Esquema no soportado: '{self.schema}'. Este Styler entiende '{PACKAGE_SCHEMA}'."
            )
        validate_identifier(self.package_id, "identificador de paquete")
        if not isinstance(self.package_type, PackageType):
            object.__setattr__(self, "package_type", PackageType(str(self.package_type)))
        if not self.name.strip():
            raise PortablePackageError("El paquete necesita un nombre visible.")
        if not _VERSION_RE.match(self.version.strip()):
            raise PortablePackageError(f"Versión de paquete inválida: '{self.version}'.")
        if not self.artifacts:
            raise PortablePackageError("Un paquete necesita al menos un artefacto.")
        identities: set[tuple[str, str]] = set()
        paths: set[str] = set()
        for artifact in self.artifacts:
            identity = (artifact.kind, artifact.artifact_id)
            if identity in identities:
                raise PortablePackageError(f"Artefacto duplicado: {artifact.kind}:{artifact.artifact_id}.")
            if artifact.path in paths:
                raise PortablePackageError(f"Ruta de artefacto duplicada: {artifact.path}.")
            identities.add(identity)
            paths.add(artifact.path)
        kinds = [item.kind for item in self.artifacts]
        if self.package_type is PackageType.BASELINE:
            if kinds != ["baseline"]:
                raise PortablePackageError(
                    "Un paquete de línea base debe contener exactamente un artefacto 'baseline'."
                )
        else:
            if kinds.count("recipe") != 1:
                raise PortablePackageError("Un paquete de cambio necesita exactamente una receta.")
            if kinds.count("graph") < 1:
                raise PortablePackageError("Un paquete de cambio necesita al menos un grafo generado.")
            if "baseline" in kinds:
                raise PortablePackageError("Una línea base no puede incrustarse como artefacto de un cambio.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "package_type": self.package_type.value,
            "package_id": self.package_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "requires_styler": self.requires_styler,
            "requires_capabilities": list(self.requires_capabilities),
            "metadata": dict(self.metadata),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PackageManifest":
        if not isinstance(data, Mapping):
            raise PortablePackageError("El manifiesto debe ser un objeto.")
        raw_artifacts = data.get("artifacts") or []
        if not isinstance(raw_artifacts, Sequence) or isinstance(raw_artifacts, (str, bytes)):
            raise PortablePackageError("'artifacts' debe ser una lista.")
        raw_caps = data.get("requires_capabilities") or []
        if not isinstance(raw_caps, Sequence) or isinstance(raw_caps, (str, bytes)):
            raise PortablePackageError("'requires_capabilities' debe ser una lista.")
        raw_metadata = data.get("metadata") or {}
        if not isinstance(raw_metadata, Mapping):
            raise PortablePackageError("'metadata' debe ser un objeto.")
        try:
            package_type = PackageType(str(data.get("package_type", "")))
        except ValueError as exc:
            raise PortablePackageError("El manifiesto necesita package_type='change' o 'baseline'.") from exc
        return cls(
            schema=str(data.get("schema", "")),
            package_type=package_type,
            package_id=str(data.get("package_id", "")),
            name=str(data.get("name", "")),
            version=str(data.get("version", "")),
            description=str(data.get("description", "")),
            author=str(data.get("author", "")),
            requires_styler=str(data.get("requires_styler", ">=0.11.0")),
            requires_capabilities=tuple(str(item) for item in raw_caps),
            metadata=dict(raw_metadata),
            artifacts=tuple(ArtifactEntry.from_dict(item) for item in raw_artifacts),
        )


@dataclass(frozen=True)
class ActionDefinition:
    action_id: str
    title: str
    action: ActionSpec
    description: str = ""
    schema: str = ACTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ACTION_SCHEMA:
            raise PortablePackageError(f"Esquema de acción no soportado: '{self.schema}'.")
        validate_identifier(self.action_id, "identificador de acción")
        if not self.title.strip():
            raise PortablePackageError("La acción necesita un título.")

    def validate(self, registry: ActionRegistry) -> None:
        registry.validate(self.action)
        audit_paths(self.action)
        audit_execution_surface(self.action)

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "action_id": self.action_id, "title": self.title,
                "description": self.description, "action": self.action.to_dict()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ActionDefinition":
        if not isinstance(data, Mapping) or not isinstance(data.get("action"), Mapping):
            raise PortablePackageError("El archivo no contiene una acción declarativa.")
        return cls(schema=str(data.get("schema", ACTION_SCHEMA)), action_id=str(data.get("action_id", "")),
                   title=str(data.get("title", "")), description=str(data.get("description", "")),
                   action=ActionSpec.from_dict(data["action"]))


@dataclass(frozen=True)
class GraphDefinition:
    graph_id: str
    title: str
    workflow: Any
    description: str = ""
    schema: str = GRAPH_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != GRAPH_SCHEMA:
            raise PortablePackageError(f"Esquema de grafo no soportado: '{self.schema}'.")
        validate_identifier(self.graph_id, "identificador de grafo")
        if not self.title.strip():
            raise PortablePackageError("El grafo necesita un título.")

    def to_dict(self) -> dict[str, Any]:
        from .workflow import workflow_to_portable_dict
        return {"schema": self.schema, "graph_id": self.graph_id, "title": self.title,
                "description": self.description, "workflow": workflow_to_portable_dict(self.workflow)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GraphDefinition":
        from .workflow import workflow_from_portable_dict
        if not isinstance(data, Mapping) or not isinstance(data.get("workflow"), Mapping):
            raise PortablePackageError("El archivo no contiene un grafo declarativo.")
        return cls(schema=str(data.get("schema", GRAPH_SCHEMA)), graph_id=str(data.get("graph_id", "")),
                   title=str(data.get("title", "")), description=str(data.get("description", "")),
                   workflow=workflow_from_portable_dict(data["workflow"]))


@dataclass(frozen=True)
class PackageInspection:
    manifest: PackageManifest
    source: str
    checksum_verified: bool
    warnings: tuple[str, ...] = ()
    collisions: tuple[str, ...] = ()
    total_files: int = 0
    total_bytes: int = 0

    @property
    def can_import(self) -> bool:
        return self.checksum_verified and not self.collisions


@dataclass(frozen=True)
class InstalledPackage:
    manifest: PackageManifest
    install_path: str
    imported_at: float
    source_checksum: str = ""

    @property
    def identity(self) -> str:
        return f"{self.manifest.package_id}@{self.manifest.version}"
