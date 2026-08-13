"""Modelos de líneas base versionadas de Styler.

Una línea base no es una imagen del disco. Es un manifiesto estructurado del
estado de una instalación concreta: distribución, edición, arquitectura,
inventario de aplicaciones y runtime mínimo de Styler.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from styler.provenance.models import Inventory, SystemIdentity

BASELINE_SCHEMA = "styler.baseline/1"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def _desktop_tokens(value: str) -> set[str]:
    aliases = {
        "x-cinnamon": "cinnamon",
        "plasma": "kde",
        "kde-plasma": "kde",
        "gnome-classic": "gnome",
    }
    tokens: set[str] = set()
    for raw in value.replace(":", ";").replace(",", ";").split(";"):
        token = raw.strip().lower()
        if not token:
            continue
        tokens.add(token)
        tokens.add(aliases.get(token, token))
    return tokens


def _desktop_matches(expected: str, actual: str) -> bool:
    return bool(_desktop_tokens(expected) & _desktop_tokens(actual))


def _major_version(value: str) -> str:
    match = re.search(r"\d+", value)
    return match.group(0) if match else value.strip().lower()


class BaselineError(Exception):
    """La línea base no es válida o no puede administrarse."""


class BaselineKind(str, Enum):
    OFFICIAL = "official"
    CUSTOM = "custom"

    @property
    def human(self) -> str:
        return {
            "official": "Oficial",
            "custom": "Personalizada",
        }[self.value]


class CompatibilityScope(str, Enum):
    """Cuánta identidad del entorno importa para un cambio concreto."""

    GENERAL = "general"
    DESKTOP = "desktop"
    SESSION = "session"


class CompatibilityStatus(str, Enum):
    VERIFIED = "verified"
    PROBABLE = "probable"
    INCOMPLETE = "incomplete"
    CONFLICT = "conflict"

    @property
    def human(self) -> str:
        return {
            "verified": "Compatibilidad verificada",
            "probable": "Compatibilidad probable",
            "incomplete": "Compatibilidad incompleta",
            "conflict": "Conflicto confirmado",
        }[self.value]


@dataclass(frozen=True)
class CompatibilityReport:
    status: CompatibilityStatus
    scope: CompatibilityScope
    conflicts: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def compatible(self) -> bool:
        return self.status is not CompatibilityStatus.CONFLICT

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "status_label": self.status.human,
            "scope": self.scope.value,
            "conflicts": list(self.conflicts),
            "missing": list(self.missing),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class RuntimeComponent:
    provider: str = ""
    version: str = ""
    executable: str = ""
    source: str = ""
    supplied_by_distro: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "version": self.version,
            "executable": self.executable,
            "source": self.source,
            "supplied_by_distro": self.supplied_by_distro,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RuntimeComponent":
        data = data or {}
        return cls(
            provider=str(data.get("provider", "")),
            version=str(data.get("version", "")),
            executable=str(data.get("executable", "")),
            source=str(data.get("source", "")),
            supplied_by_distro=bool(data.get("supplied_by_distro", False)),
        )


@dataclass(frozen=True)
class RuntimeProfile:
    python: RuntimeComponent = field(default_factory=RuntimeComponent)
    environment: RuntimeComponent = field(default_factory=RuntimeComponent)
    rust: RuntimeComponent = field(default_factory=RuntimeComponent)
    styler: RuntimeComponent = field(default_factory=RuntimeComponent)

    def to_dict(self) -> dict[str, Any]:
        return {
            "python": self.python.to_dict(),
            "environment": self.environment.to_dict(),
            "rust": self.rust.to_dict(),
            "styler": self.styler.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RuntimeProfile":
        data = data or {}
        return cls(
            python=RuntimeComponent.from_dict(data.get("python")),
            environment=RuntimeComponent.from_dict(data.get("environment")),
            rust=RuntimeComponent.from_dict(data.get("rust")),
            styler=RuntimeComponent.from_dict(data.get("styler")),
        )


@dataclass(frozen=True)
class ImageIdentity:
    installation_profile: str = "default"
    image_name: str = ""
    image_checksum: str = ""
    updates_policy: str = "captured-state"
    clean_install: bool = False
    captured_after_updates: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "installation_profile": self.installation_profile,
            "image_name": self.image_name,
            "image_checksum": self.image_checksum,
            "updates_policy": self.updates_policy,
            "clean_install": self.clean_install,
            "captured_after_updates": self.captured_after_updates,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ImageIdentity":
        data = data or {}
        return cls(
            installation_profile=str(data.get("installation_profile", "default")),
            image_name=str(data.get("image_name", "")),
            image_checksum=str(data.get("image_checksum", "")),
            updates_policy=str(data.get("updates_policy", "captured-state")),
            clean_install=bool(data.get("clean_install", False)),
            captured_after_updates=bool(data.get("captured_after_updates", False)),
        )


@dataclass(frozen=True)
class BaselineDefinition:
    baseline_id: str
    name: str
    kind: BaselineKind
    inventory: Inventory
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    created_at: float = field(default_factory=time.time)
    image: ImageIdentity = field(default_factory=ImageIdentity)
    runtime: RuntimeProfile = field(default_factory=RuntimeProfile)
    trusted: bool = False
    source: str = "local-capture"
    warnings: tuple[str, ...] = ()
    schema: str = BASELINE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != BASELINE_SCHEMA:
            raise BaselineError(f"Esquema de línea base no soportado: {self.schema}")
        if not _ID_RE.match(self.baseline_id):
            raise BaselineError(
                "Identificador de línea base inválido; usa minúsculas, dígitos, punto, guion o guion bajo."
            )
        if not self.name.strip():
            raise BaselineError("La línea base necesita un nombre visible.")
        if self.kind is BaselineKind.OFFICIAL and not self.image.clean_install:
            raise BaselineError(
                "Una línea base oficial debe declarar que procede de una instalación limpia."
            )

    @property
    def system(self) -> SystemIdentity:
        return self.inventory.system

    @property
    def is_official(self) -> bool:
        return self.kind is BaselineKind.OFFICIAL

    @property
    def label(self) -> str:
        suffix = "oficial" if self.is_official else "personalizada"
        return f"{self.name} ({suffix})"

    def matches_default_identity(self, system: SystemIdentity) -> bool:
        """¿Esta baseline puede ser el *default* de este sistema exacto?

        Un default oficial nunca es global ni un fallback entre distribuciones.
        Cada baseline pertenece a la identidad que declara. Los campos que la
        baseline conoce son requisitos: si el equipo no puede demostrar uno de
        ellos, no se adopta automáticamente.
        """
        mine = self.system
        required_exact = (
            (mine.distro_id, system.distro_id),
            (mine.distro_version, system.distro_version),
            (mine.architecture, system.architecture),
            (mine.distro_variant, system.distro_variant),
            (mine.session_type, system.session_type),
            (mine.release_model, system.release_model),
            (mine.build_id, system.build_id),
        )
        for expected, actual in required_exact:
            if expected and (not actual or expected.lower() != actual.lower()):
                return False

        if not (mine.distro_id and mine.distro_version and mine.architecture):
            return False
        if mine.desktop and (not system.desktop or not _desktop_matches(mine.desktop, system.desktop)):
            return False
        if mine.desktop_version:
            if not system.desktop_version:
                return False
            if _major_version(mine.desktop_version) != _major_version(system.desktop_version):
                return False
        return True

    def compatibility_score(self, system: SystemIdentity) -> int:
        """Puntuación entre baselines oficiales del MISMO destino exacto.

        ``matches_default_identity`` decide primero si una baseline pertenece a
        este sistema. La puntuación solo desempata varias capturas válidas para
        esa misma identidad; nunca permite usar Mint como default de Ubuntu,
        Debian, Arch, otra sesión o una edición distinta.
        """
        if not self.matches_default_identity(system):
            return -1

        mine = self.system
        score = 100
        if mine.distro_variant:
            score += 20
        if mine.desktop:
            score += 10
        if mine.desktop_version:
            score += 5
        if mine.session_type:
            score += 5
        if mine.release_model:
            score += 5
        if mine.build_id:
            score += 5
        if self.is_official:
            score += 50
        if self.trusted:
            score += 25
        return score

    def compatibility_report(
        self,
        system: SystemIdentity,
        *,
        scope: CompatibilityScope = CompatibilityScope.GENERAL,
    ) -> CompatibilityReport:
        """Compara una base y un destino sin tratar lo desconocido como error.

        El alcance permite que una aplicación normal ignore el escritorio,
        mientras un tema o una extensión exijan escritorio y tipo de sesión.
        """
        mine = self.system
        conflicts: list[str] = []
        missing: list[str] = []
        notes: list[str] = []

        pairs = (
            ("distribución", mine.distro_id, system.distro_id),
            ("versión", mine.distro_version, system.distro_version),
            ("arquitectura", mine.architecture, system.architecture),
            ("edición", mine.distro_variant, system.distro_variant),
        )
        for label, expected, actual in pairs:
            if expected and actual:
                if expected.lower() != actual.lower():
                    conflicts.append(
                        f"La línea base declara {label} «{expected}» y el destino tiene «{actual}»."
                    )
            elif label in {"distribución", "versión", "arquitectura"}:
                missing.append(label)
            elif expected or actual:
                notes.append(f"No se pudo comprobar la {label} en ambos lados.")

        if mine.release_model and system.release_model:
            if mine.release_model.lower() != system.release_model.lower():
                notes.append(
                    f"La base usa un modelo «{mine.release_model}» y el destino «{system.release_model}»."
                )

        if scope in {CompatibilityScope.DESKTOP, CompatibilityScope.SESSION}:
            if mine.desktop and system.desktop:
                if not _desktop_matches(mine.desktop, system.desktop):
                    conflicts.append(
                        f"La línea base usa el escritorio «{mine.desktop}» y el destino «{system.desktop}»."
                    )
            else:
                missing.append("escritorio")

            if mine.desktop_version and system.desktop_version:
                if _major_version(mine.desktop_version) != _major_version(system.desktop_version):
                    conflicts.append(
                        "La versión mayor del escritorio no coincide "
                        f"(«{mine.desktop_version}» frente a «{system.desktop_version}»)."
                    )
            elif mine.desktop_version or system.desktop_version:
                missing.append("versión del escritorio")

        if scope is CompatibilityScope.SESSION:
            if mine.session_type and system.session_type:
                if mine.session_type.lower() != system.session_type.lower():
                    conflicts.append(
                        f"La sesión es «{mine.session_type}» en la base y «{system.session_type}» en el destino."
                    )
            else:
                missing.append("tipo de sesión")

        if conflicts:
            status = CompatibilityStatus.CONFLICT
        elif missing:
            status = CompatibilityStatus.INCOMPLETE
        elif notes:
            status = CompatibilityStatus.PROBABLE
        else:
            status = CompatibilityStatus.VERIFIED
        return CompatibilityReport(
            status=status,
            scope=scope,
            conflicts=tuple(dict.fromkeys(conflicts)),
            missing=tuple(dict.fromkeys(missing)),
            notes=tuple(dict.fromkeys(notes)),
        )

    def conflicts_with(
        self,
        system: SystemIdentity,
        *,
        scope: CompatibilityScope = CompatibilityScope.GENERAL,
    ) -> str:
        """Motivo por el que esta base NO sirve para ese sistema, o cadena vacía.

        Es deliberadamente distinto de ``compatibility_score``. Recomendar una
        base oficial exige coincidencia demostrada; en cambio, bloquear una
        exportación exige una discrepancia demostrada. Un campo vacío significa
        «no se sabe», no «no coincide»: es el mismo criterio que ya aplica
        ``provenance.baseline.compare()``, que ante una identidad incompleta
        advierte en vez de rechazar. Sin esta distinción, cualquier línea base
        migrada de 0.6.5 sin identidad de sistema quedaría inexportable.
        """
        report = self.compatibility_report(system, scope=scope)
        if not report.conflicts:
            return ""
        return f"La línea base «{self.name}» no coincide: {report.conflicts[0]}"

    def incomplete_identity(self) -> bool:
        """¿La base no declara identidad suficiente para comprobar nada?"""
        mine = self.system
        return not (mine.distro_id and mine.distro_version and mine.architecture)

    def to_dict(self, *, include_inventory: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema": self.schema,
            "baseline_id": self.baseline_id,
            "name": self.name,
            "kind": self.kind.value,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "created_at": self.created_at,
            "system": self.system.to_dict(),
            "image": self.image.to_dict(),
            "runtime": self.runtime.to_dict(),
            "trusted": self.trusted,
            "source": self.source,
            "warnings": list(self.warnings),
            "inventory_id": self.inventory.inventory_id,
        }
        if include_inventory:
            data["inventory"] = self.inventory.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, inventory: Inventory | None = None) -> "BaselineDefinition":
        raw_inventory = inventory
        if raw_inventory is None:
            value = data.get("inventory")
            if not isinstance(value, Mapping):
                raise BaselineError("La línea base no contiene un inventario.")
            raw_inventory = Inventory.from_dict(dict(value))
        return cls(
            schema=str(data.get("schema", BASELINE_SCHEMA)),
            baseline_id=str(data.get("baseline_id", "")),
            name=str(data.get("name", "")),
            kind=BaselineKind(str(data.get("kind", BaselineKind.CUSTOM.value))),
            inventory=raw_inventory,
            version=str(data.get("version", "1.0.0")),
            description=str(data.get("description", "")),
            author=str(data.get("author", "")),
            created_at=float(data.get("created_at", time.time())),
            image=ImageIdentity.from_dict(data.get("image") if isinstance(data.get("image"), Mapping) else {}),
            runtime=RuntimeProfile.from_dict(data.get("runtime") if isinstance(data.get("runtime"), Mapping) else {}),
            trusted=bool(data.get("trusted", False)),
            source=str(data.get("source", "imported")),
            warnings=tuple(str(item) for item in data.get("warnings", []) or []),
        )
