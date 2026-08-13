"""Perfiles: unidad principal que se comparte, previsualiza y aplica."""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from styler.layers import Layer, load_layer
from styler.models import FileEntry
from styler.parts import title_for
from styler.validation import ValidationError, safe_record_path, validate_identifier, validate_logical_path

STYLER_DIR = ".styler"
PROFILES_DIR = os.path.join(STYLER_DIR, "profiles")


@dataclass
class Conflict:
    path: str
    part_id: str
    layer_ids: list[str]
    winner: str

    def human_message(self) -> str:
        return (
            f"«{title_for(self.part_id)}» ya está configurada de otra manera. "
            "Elige cuál conservar antes de aplicar o compartir."
        )


@dataclass
class ConflictResolution:
    strategy: str  # choose_layer | follow_layer_order | exclude_path
    selected_layer_id: str = ""

    def validate(self, available_layers: list[str]) -> None:
        if self.strategy not in {"choose_layer", "follow_layer_order", "exclude_path"}:
            raise ValidationError(f"Estrategia de conflicto no permitida: {self.strategy}")
        if self.strategy == "choose_layer":
            validate_identifier(self.selected_layer_id, "Capa elegida")
            if self.selected_layer_id not in available_layers:
                raise ValidationError("La capa elegida no participa en el conflicto.")


@dataclass
class Profile:
    profile_id: str
    name: str
    created_at: float = field(default_factory=time.time)
    layer_ids: list[str] = field(default_factory=list)
    based_on: str = ""
    notes: str = ""
    resolutions: dict[str, ConflictResolution] = field(default_factory=dict)

    def validate(self) -> None:
        validate_identifier(self.profile_id, "ID de perfil")
        if not self.name.strip():
            raise ValidationError("El perfil necesita un nombre.")
        for layer_id in self.layer_ids:
            validate_identifier(layer_id, "ID de capa")
        if len(self.layer_ids) != len(set(self.layer_ids)):
            raise ValidationError("El perfil no puede repetir una misma capa.")
        for path, resolution in self.resolutions.items():
            validate_logical_path(path)
            if resolution.strategy == "choose_layer":
                validate_identifier(resolution.selected_layer_id, "Capa elegida")

    def to_dict(self) -> dict:
        self.validate()
        return {
            "schema_version": 2,
            "profile_id": self.profile_id,
            "name": self.name,
            "created_at": self.created_at,
            "layer_ids": list(self.layer_ids),
            "based_on": self.based_on,
            "notes": self.notes,
            "resolutions": {path: asdict(value) for path, value in self.resolutions.items()},
        }

    @staticmethod
    def from_dict(d: dict) -> "Profile":
        raw_resolutions = d.get("resolutions", {}) or {}
        profile = Profile(
            profile_id=d["profile_id"],
            name=d.get("name", ""),
            created_at=d.get("created_at", time.time()),
            layer_ids=list(d.get("layer_ids", [])),
            based_on=d.get("based_on", ""),
            notes=d.get("notes", ""),
            resolutions={
                path: ConflictResolution(**value)
                for path, value in raw_resolutions.items()
            },
        )
        profile.validate()
        return profile


def create_profile(name: str, layer_ids: list[str], based_on: str = "") -> Profile:
    profile = Profile(
        profile_id=f"profile-{uuid.uuid4().hex[:8]}",
        name=name,
        layer_ids=list(layer_ids),
        based_on=based_on,
    )
    profile.validate()
    return profile


def detect_conflicts(layers: list[Layer]) -> list[Conflict]:
    by_path: dict[str, list[tuple[str, str, str]]] = {}
    for layer in layers:
        for entry in layer.files:
            by_path.setdefault(entry.path, []).append((layer.layer_id, entry.checksum, layer.part_id))

    conflicts: list[Conflict] = []
    for path, claims in by_path.items():
        if len(claims) < 2 or len({checksum for _lid, checksum, _part in claims}) < 2:
            continue
        conflicts.append(
            Conflict(
                path=path,
                part_id=claims[-1][2],
                layer_ids=[layer_id for layer_id, _checksum, _part in claims],
                winner=claims[-1][0],
            )
        )
    return sorted(conflicts, key=lambda conflict: conflict.path)


def unresolved_conflicts(profile: Profile, layers: list[Layer]) -> list[Conflict]:
    unresolved: list[Conflict] = []
    for conflict in detect_conflicts(layers):
        resolution = profile.resolutions.get(conflict.path)
        if resolution is None:
            unresolved.append(conflict)
            continue
        try:
            resolution.validate(conflict.layer_ids)
        except ValidationError:
            unresolved.append(conflict)
    return unresolved


def resolve_conflict(
    profile: Profile,
    path: str,
    strategy: str,
    selected_layer_id: str = "",
) -> Profile:
    validate_logical_path(path)
    profile.resolutions[path] = ConflictResolution(strategy, selected_layer_id)
    profile.validate()
    return profile


def compose(layers: list[Layer]) -> list[FileEntry]:
    """Composición simple: la última capa gana. Úsese solo para datos sin conflicto."""
    resolved: dict[str, FileEntry] = {}
    for layer in layers:
        for entry in layer.files:
            resolved[entry.path] = entry
    return [resolved[path] for path in sorted(resolved)]


def compose_applications(layers: list[Layer]) -> list:
    """Aplicaciones que este perfil instala, sin duplicados entre capas."""
    from styler.applications import merge_applications

    return merge_applications(layer.applications for layer in layers)


def compose_profile(profile: Profile, layers: list[Layer]) -> list[FileEntry]:
    conflicts = {conflict.path: conflict for conflict in detect_conflicts(layers)}
    unresolved = unresolved_conflicts(profile, layers)
    if unresolved:
        raise ValueError("El perfil tiene conflictos sin resolver: " + ", ".join(c.path for c in unresolved))

    claims: dict[str, list[tuple[str, FileEntry]]] = {}
    for layer in layers:
        for entry in layer.files:
            claims.setdefault(entry.path, []).append((layer.layer_id, entry))

    result: list[FileEntry] = []
    for path in sorted(claims):
        options = claims[path]
        if path not in conflicts:
            result.append(options[-1][1])
            continue
        resolution = profile.resolutions[path]
        resolution.validate([layer_id for layer_id, _entry in options])
        if resolution.strategy == "exclude_path":
            continue
        if resolution.strategy == "follow_layer_order":
            result.append(options[-1][1])
            continue
        selected = next(entry for layer_id, entry in options if layer_id == resolution.selected_layer_id)
        result.append(selected)
    return result


def load_profile_layers(profile: Profile, root: str = ".") -> list[Layer]:
    return [load_layer(layer_id, root) for layer_id in profile.layer_ids]


def unapplicable_layers(layers: list[Layer]) -> list[Layer]:
    return [layer for layer in layers if not layer.restorable()]


def _profiles_dir(root: str) -> Path:
    path = Path(root) / PROFILES_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_profile(profile: Profile, root: str = ".", overwrite: bool = True) -> str:
    profile.validate()
    path = safe_record_path(_profiles_dir(root), profile.profile_id)
    if path.exists() and not overwrite:
        raise FileExistsError(f"Ya existe un perfil con ID {profile.profile_id}.")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return str(path)


def load_profile(profile_id: str, root: str = ".") -> Profile:
    path = safe_record_path(Path(root) / PROFILES_DIR, profile_id)
    try:
        return Profile.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise ValueError(f"El perfil {profile_id} está dañado.") from exc


def list_profiles(root: str = ".") -> list[str]:
    directory = Path(root) / PROFILES_DIR
    if not directory.is_dir():
        return []
    result: list[str] = []
    for path in directory.glob("*.json"):
        try:
            result.append(validate_identifier(path.stem, "ID de perfil"))
        except ValidationError:
            continue
    return sorted(result)


def delete_profile(profile_id: str, root: str = ".") -> None:
    """Elimina una configuración de la biblioteca.

    Solo desaparece el perfil: las capas y los objetos por contenido siguen en
    el almacén, porque otras configuraciones pueden compartirlos. Borrar una
    configuración importada nunca toca el escritorio ya aplicado; para eso está
    «Deshacer» en el registro de cambios.
    """
    path = safe_record_path(Path(root) / PROFILES_DIR, profile_id)
    if not path.is_file():
        raise ValidationError(f"No existe la configuración: {profile_id}")
    path.unlink()
