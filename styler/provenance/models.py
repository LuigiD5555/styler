"""
styler.provenance.models
========================
Modelo central de procedencia de aplicaciones (Styler 0.13.1).

Reglas del modelo:

* Un registro describe **una aplicación instalada** y **de dónde salió**.
* El "remote de la aplicación" (APT, Flatpak, Snap, pacman, RPM, AppImage) NO
  es el mismo concepto que un "remote de Git". Aquí solo existe el primero.
* Todo dato tiene un nivel de confianza explícito. Styler nunca afirma haber
  encontrado el repositorio oficial de una aplicación solo porque el nombre se
  parece.
* Esta capa es de SOLO LECTURA: no descarga, no instala, no toca la red.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Confidence(str, Enum):
    """Nivel de evidencia detrás de un dato de procedencia."""

    CONFIRMED = "confirmed"   # el gestor de paquetes entregó el dato
    INFERRED = "inferred"     # el paquete declara Homepage/Source/URL
    SUGGESTED = "suggested"   # hay una pista razonable, sin confirmar
    UNKNOWN = "unknown"       # no hay evidencia suficiente

    @property
    def rank(self) -> int:
        return {"confirmed": 3, "inferred": 2, "suggested": 1, "unknown": 0}[self.value]

    @property
    def human(self) -> str:
        return {
            "confirmed": "Confirmado",
            "inferred": "Inferido",
            "suggested": "Sugerido",
            "unknown": "Desconocido",
        }[self.value]


class BaselineRole(str, Enum):
    """Relación de una aplicación con una línea base capturada."""

    BASE = "base"
    ADDED = "added-since-baseline"
    REMOVED = "removed-from-baseline"
    CHANGED = "changed-since-baseline"
    UNKNOWN = "unknown"


class InstallReason(str, Enum):
    """Razón declarada por el gestor, cuando puede conocerse."""

    EXPLICIT = "explicit"
    DEPENDENCY = "dependency"
    PORTABLE = "portable"
    LOCAL = "local"
    UNKNOWN = "unknown"


class OriginKind(str, Enum):
    """Tipo de remote de aplicación."""

    APT = "apt"
    FLATPAK = "flatpak"
    SNAP = "snap"
    PACMAN = "pacman"
    RPM = "rpm"
    APPIMAGE = "appimage"
    MANUAL = "manual"
    # Usados por el monitor de cambios contra línea base.
    AUR = "aur"
    ZYPPER = "zypper"
    NIX = "nix"
    BREW = "brew"
    CONTAINER = "container"
    PIPX = "pipx"
    CARGO = "cargo"
    NPM = "npm"
    CONDA = "conda"
    UNKNOWN = "unknown"


class AppCategory(str, Enum):
    """Qué clase de cosa es lo detectado.

    No es lo mismo una aplicación de escritorio que un contenedor: la forma de
    reproducirlos en otra máquina es distinta y la política de exportación
    también. Algunos detectores no rellenan este campo; para
    ellos queda ``UNKNOWN`` y la clasificación se infiere después.
    """

    DESKTOP_APP = "desktop-app"
    DEV_TOOL = "dev-tool"
    ISOLATED_ENV = "isolated-env"
    SYSTEM_BASE = "system-base"
    UNKNOWN = "unknown"

    @property
    def human(self) -> str:
        return {
            "desktop-app": "Aplicación",
            "dev-tool": "Herramienta de desarrollo",
            "isolated-env": "Entorno aislado",
            "system-base": "Base del sistema",
            "unknown": "Sin clasificar",
        }[self.value]


class ArtifactKind(str, Enum):
    """Tipo de recurso visual o de configuración observado."""

    THEME = "theme"
    ICON_THEME = "icon-theme"
    CURSOR_THEME = "cursor-theme"
    WALLPAPER = "wallpaper"
    FONT = "font"
    CSS = "css"
    CONFIG = "config"
    SETTING = "setting"
    OTHER = "other"

    @property
    def human(self) -> str:
        return {
            "theme": "Tema",
            "icon-theme": "Tema de iconos",
            "cursor-theme": "Cursor",
            "wallpaper": "Fondo",
            "font": "Fuente",
            "css": "CSS visual",
            "config": "Configuración",
            "setting": "Ajuste visual",
            "other": "Archivo",
        }[self.value]


@dataclass
class SystemArtifactRecord:
    """Archivo o directorio visual/configurable que puede formar parte de un cambio."""

    artifact_id: str
    kind: ArtifactKind
    name: str
    path: str
    checksum: str
    size: int = 0
    mode: int = 0
    is_directory: bool = False
    file_count: int = 1
    scope: str = "user"
    setting_backend: str = ""
    setting_schema: str = ""
    setting_group: str = ""
    setting_key: str = ""
    setting_value: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind.value,
            "name": self.name,
            "path": self.path,
            "checksum": self.checksum,
            "size": self.size,
            "mode": self.mode,
            "is_directory": self.is_directory,
            "file_count": self.file_count,
            "scope": self.scope,
            "setting_backend": self.setting_backend,
            "setting_schema": self.setting_schema,
            "setting_group": self.setting_group,
            "setting_key": self.setting_key,
            "setting_value": self.setting_value,
            "warnings": list(self.warnings),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SystemArtifactRecord":
        return SystemArtifactRecord(
            artifact_id=str(data["artifact_id"]),
            kind=ArtifactKind(str(data.get("kind", "other"))),
            name=str(data.get("name", "")),
            path=str(data.get("path", "")),
            checksum=str(data.get("checksum", "")),
            size=int(data.get("size", 0)),
            mode=int(data.get("mode", 0)),
            is_directory=bool(data.get("is_directory", False)),
            file_count=int(data.get("file_count", 1)),
            scope=str(data.get("scope", "user")),
            setting_backend=str(data.get("setting_backend", "")),
            setting_schema=str(data.get("setting_schema", "")),
            setting_group=str(data.get("setting_group", "")),
            setting_key=str(data.get("setting_key", "")),
            setting_value=str(data.get("setting_value", "")),
            warnings=[str(item) for item in data.get("warnings", [])],
        )


@dataclass
class Origin:
    """De dónde vino el binario instalado."""

    kind: OriginKind = OriginKind.UNKNOWN
    remote_name: str = ""      # flathub, jammy/main, extra, snap store...
    remote_url: str = ""       # http://archive.ubuntu.com/ubuntu, https://dl.flathub.org/repo/
    branch: str = ""           # stable, jammy-updates, latest/stable
    ref: str = ""              # app/org.mozilla.firefox/x86_64/stable
    commit: str = ""           # commit OSTree o revisión Snap
    channel: str = ""          # stable / candidate / beta / edge
    vendor: str = ""           # publisher / maintainer / vendor
    source_package: str = ""   # paquete fuente cuando el gestor lo declara
    signed: Optional[bool] = None   # None = no se pudo determinar
    confidence: Confidence = Confidence.UNKNOWN
    evidence: str = ""         # comando o archivo del que salió el dato

    def to_dict(self) -> dict[str, Any]:
        data = {
            "kind": self.kind.value,
            "remote_name": self.remote_name,
            "remote_url": self.remote_url,
            "branch": self.branch,
            "ref": self.ref,
            "commit": self.commit,
            "channel": self.channel,
            "vendor": self.vendor,
            "source_package": self.source_package,
            "signed": self.signed,
            "confidence": self.confidence.value,
            "evidence": self.evidence,
        }
        return data

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Origin":
        return Origin(
            kind=OriginKind(data.get("kind", "unknown")),
            remote_name=data.get("remote_name", ""),
            remote_url=data.get("remote_url", ""),
            branch=data.get("branch", ""),
            ref=data.get("ref", ""),
            commit=data.get("commit", ""),
            channel=data.get("channel", ""),
            vendor=data.get("vendor", ""),
            source_package=data.get("source_package", ""),
            signed=data.get("signed", None),
            confidence=Confidence(data.get("confidence", "unknown")),
            evidence=data.get("evidence", ""),
        )


@dataclass
class Upstream:
    """Repositorio de la persona o proyecto que desarrolla la aplicación.

    `packaging_repository` es distinto: es el repositorio que empaqueta, no el
    que desarrolla (por ejemplo github.com/flathub/<app-id>). Styler los separa
    a propósito para no mentirle a nadie.
    """

    provider: str = ""            # github, gitlab, codeberg, sourcehut, other
    repository: str = ""          # owner/name
    url: str = ""                 # URL del repositorio
    homepage: str = ""            # Homepage declarada por el paquete
    releases_url: str = ""        # solo si el gestor o el metadato la declara
    packaging_repository: str = ""  # repositorio de empaquetado, si aplica
    confidence: Confidence = Confidence.UNKNOWN
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "repository": self.repository,
            "url": self.url,
            "homepage": self.homepage,
            "releases_url": self.releases_url,
            "packaging_repository": self.packaging_repository,
            "confidence": self.confidence.value,
            "evidence": self.evidence,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Upstream":
        return Upstream(
            provider=data.get("provider", ""),
            repository=data.get("repository", ""),
            url=data.get("url", ""),
            homepage=data.get("homepage", ""),
            releases_url=data.get("releases_url", ""),
            packaging_repository=data.get("packaging_repository", ""),
            confidence=Confidence(data.get("confidence", "unknown")),
            evidence=data.get("evidence", ""),
        )


@dataclass
class Integrity:
    """Verificabilidad del artefacto instalado."""

    checksum: str = ""              # sha256:... del archivo, cuando existe archivo
    signature_verified: Optional[bool] = None
    key_fingerprint: str = ""
    artifact_path: str = ""         # ruta local del artefacto, si Styler la conoce
    artifact_available: bool = False  # ¿se puede reinstalar sin red hoy mismo?

    def to_dict(self) -> dict[str, Any]:
        return {
            "checksum": self.checksum,
            "signature_verified": self.signature_verified,
            "key_fingerprint": self.key_fingerprint,
            "artifact_path": self.artifact_path,
            "artifact_available": self.artifact_available,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Integrity":
        return Integrity(
            checksum=data.get("checksum", ""),
            signature_verified=data.get("signature_verified", None),
            key_fingerprint=data.get("key_fingerprint", ""),
            artifact_path=data.get("artifact_path", ""),
            artifact_available=bool(data.get("artifact_available", False)),
        )


@dataclass
class ApplicationRecord:
    """Una aplicación instalada, con su procedencia."""

    app_id: str                     # identificador estable: "<manager>:<name>"
    name: str                       # nombre canónico según el gestor
    display_name: str = ""          # nombre humano cuando se conoce
    manager: str = ""               # apt, flatpak, snap, pacman, rpm, appimage
    version: str = ""
    architecture: str = ""
    install_method: str = ""        # repository, manual, bundle, appimage
    install_reason: InstallReason = InstallReason.UNKNOWN
    baseline_role: BaselineRole = BaselineRole.UNKNOWN
    category: AppCategory = AppCategory.UNKNOWN
    origin: Origin = field(default_factory=Origin)
    upstream: Upstream = field(default_factory=Upstream)
    integrity: Integrity = field(default_factory=Integrity)
    warnings: list[str] = field(default_factory=list)

    @property
    def reproducible_today(self) -> bool:
        """¿Styler podría volver a instalar exactamente esto hoy?

        Solo es cierto si conocemos un remote con confianza confirmada o si
        tenemos el artefacto real en disco. Una URL viva no es garantía, pero
        una URL desconocida sí es garantía de que NO se puede.
        """
        if self.integrity.artifact_available:
            return True
        return (
            self.origin.confidence == Confidence.CONFIRMED
            and bool(self.origin.remote_name or self.origin.remote_url)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "name": self.name,
            "display_name": self.display_name,
            "manager": self.manager,
            "version": self.version,
            "architecture": self.architecture,
            "install_method": self.install_method,
            "install_reason": self.install_reason.value,
            "baseline_role": self.baseline_role.value,
            "category": self.category.value,
            "origin": self.origin.to_dict(),
            "upstream": self.upstream.to_dict(),
            "integrity": self.integrity.to_dict(),
            "warnings": list(self.warnings),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ApplicationRecord":
        return ApplicationRecord(
            app_id=data["app_id"],
            name=data.get("name", ""),
            display_name=data.get("display_name", ""),
            manager=data.get("manager", ""),
            version=data.get("version", ""),
            architecture=data.get("architecture", ""),
            install_method=data.get("install_method", ""),
            install_reason=InstallReason(data.get("install_reason", "unknown")),
            baseline_role=BaselineRole(data.get("baseline_role", "unknown")),
            category=AppCategory(data.get("category", "unknown")),
            origin=Origin.from_dict(data.get("origin", {})),
            upstream=Upstream.from_dict(data.get("upstream", {})),
            integrity=Integrity.from_dict(data.get("integrity", {})),
            warnings=list(data.get("warnings", [])),
        )


@dataclass
class SystemIdentity:
    """Identidad suficiente para saber qué significa una línea base."""

    distro_id: str = ""
    distro_version: str = ""
    distro_variant: str = ""
    architecture: str = ""
    desktop: str = ""
    desktop_version: str = ""
    session_type: str = ""
    release_model: str = ""
    build_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "distro_id": self.distro_id,
            "distro_version": self.distro_version,
            "distro_variant": self.distro_variant,
            "architecture": self.architecture,
            "desktop": self.desktop,
            "desktop_version": self.desktop_version,
            "session_type": self.session_type,
            "release_model": self.release_model,
            "build_id": self.build_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SystemIdentity":
        return SystemIdentity(
            distro_id=data.get("distro_id", ""),
            distro_version=data.get("distro_version", ""),
            distro_variant=data.get("distro_variant", ""),
            architecture=data.get("architecture", ""),
            desktop=data.get("desktop", ""),
            desktop_version=data.get("desktop_version", ""),
            session_type=data.get("session_type", ""),
            release_model=data.get("release_model", ""),
            build_id=data.get("build_id", ""),
        )

    def comparable_key(self) -> tuple[str, str, str, str]:
        return (
            self.distro_id.lower(),
            self.distro_version,
            self.distro_variant.lower(),
            self.architecture.lower(),
        )


INVENTORY_SCHEMA = "styler.provenance/4"


@dataclass
class Inventory:
    """Catálogo de procedencia de una máquina, en un momento dado."""

    inventory_id: str
    captured_at: float = field(default_factory=time.time)
    distro: str = ""
    system: SystemIdentity = field(default_factory=SystemIdentity)
    scope: str = "apps"             # apps | all
    managers_seen: list[str] = field(default_factory=list)
    applications: list[ApplicationRecord] = field(default_factory=list)
    artifacts: list[SystemArtifactRecord] = field(default_factory=list)

    def by_id(self, app_id: str) -> Optional[ApplicationRecord]:
        for record in self.applications:
            if record.app_id == app_id:
                return record
        return None

    def find(self, needle: str) -> list[ApplicationRecord]:
        needle = needle.strip().lower()
        return [
            record
            for record in self.applications
            if needle in record.app_id.lower() or needle in record.name.lower()
        ]

    def needs_attention(self) -> list[ApplicationRecord]:
        """Aplicaciones que hoy NO podrían reconstruirse con certeza."""
        return [record for record in self.applications if not record.reproducible_today]

    def counts_by_manager(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.applications:
            counts[record.manager] = counts.get(record.manager, 0) + 1
        return dict(sorted(counts.items()))

    def counts_by_confidence(self) -> dict[str, int]:
        counts = {level.value: 0 for level in Confidence}
        for record in self.applications:
            counts[record.origin.confidence.value] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": INVENTORY_SCHEMA,
            "inventory_id": self.inventory_id,
            "captured_at": self.captured_at,
            "distro": self.distro,
            "system": self.system.to_dict(),
            "scope": self.scope,
            "managers_seen": list(self.managers_seen),
            "applications": [record.to_dict() for record in self.applications],
            "artifacts": [record.to_dict() for record in self.artifacts],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Inventory":
        schema = data.get("schema", INVENTORY_SCHEMA)
        if schema != INVENTORY_SCHEMA:
            raise ValueError(f"Esquema de inventario no soportado: {schema}")
        return Inventory(
            inventory_id=data["inventory_id"],
            captured_at=float(data.get("captured_at", time.time())),
            distro=data.get("distro", ""),
            system=SystemIdentity.from_dict(data.get("system", {})),
            scope=data.get("scope", "apps"),
            managers_seen=list(data.get("managers_seen", [])),
            applications=[ApplicationRecord.from_dict(item) for item in data.get("applications", [])],
            artifacts=[SystemArtifactRecord.from_dict(item) for item in data.get("artifacts", [])],
        )
