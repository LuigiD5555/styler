"""Capas reutilizables de una personalización."""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from styler.applications import AppSpec
from styler.dependencies import infer_packages
from styler.models import DesktopEnvironmentRecord, FileEntry, Package
from styler.parts import APPLICATIONS_PART, classify, title_for
from styler.snapshot import Snapshot
from styler.validation import (
    ValidationError,
    safe_record_path,
    validate_checksum,
    validate_identifier,
    validate_logical_path,
)

STYLER_DIR = ".styler"
LAYERS_DIR = os.path.join(STYLER_DIR, "layers")


@dataclass
class Layer:
    layer_id: str
    part_id: str
    title: str
    created_at: float = field(default_factory=time.time)
    source_snapshot: str = ""
    based_on: str = ""
    desktop: str = ""
    desktop_environments: list[DesktopEnvironmentRecord] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)
    packages: list[Package] = field(default_factory=list)
    # Aplicaciones que esta capa instala al aplicarse (ver styler/applications.py).
    applications: list[AppSpec] = field(default_factory=list)
    notes: str = ""

    def validate(self) -> None:
        validate_identifier(self.layer_id, "ID de capa")
        validate_identifier(self.part_id, "ID de parte")
        if self.source_snapshot:
            validate_identifier(self.source_snapshot, "ID de configuración de origen")
        for entry in self.files:
            validate_logical_path(entry.path)
            validate_checksum(entry.checksum)

    def to_dict(self) -> dict:
        self.validate()
        return {
            "schema_version": 3,
            "layer_id": self.layer_id,
            "part_id": self.part_id,
            "title": self.title,
            "created_at": self.created_at,
            "source_snapshot": self.source_snapshot,
            "based_on": self.based_on,
            "desktop": self.desktop,
            "desktop_environments": [item.to_dict() for item in self.desktop_environments],
            "files": [entry.to_dict() for entry in self.files],
            "packages": [asdict(package) for package in self.packages],
            "applications": [app.to_dict() for app in self.applications],
            "notes": self.notes,
        }

    @staticmethod
    def from_dict(d: dict) -> "Layer":
        try:
            layer = Layer(
                layer_id=d["layer_id"],
                part_id=d["part_id"],
                title=d.get("title", ""),
                created_at=d.get("created_at", time.time()),
                source_snapshot=d.get("source_snapshot", ""),
                based_on=d.get("based_on", ""),
                desktop=d.get("desktop", ""),
                desktop_environments=[
                    DesktopEnvironmentRecord.from_dict(item)
                    for item in d.get("desktop_environments", [])
                ],
                files=[FileEntry.from_dict(f) for f in d.get("files", [])],
                packages=[Package(**p) for p in d.get("packages", [])],
                applications=[AppSpec.from_dict(a) for a in d.get("applications", [])],
                notes=d.get("notes", ""),
            )
            layer.validate()
            return layer
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ValueError(f"Capa inválida: {exc}") from exc

    def paths(self) -> list[str]:
        return [entry.path for entry in self.files]

    def restorable(self) -> bool:
        """Indica que todas las referencias son estructuralmente válidas.

        La existencia e integridad real se comprueban contra ObjectStore en el
        preflight de aplicación o exportación.
        """
        if not self.files and not self.applications:
            return False
        try:
            self.validate()
            return True
        except (ValidationError, ValueError):
            return False


def extract_layers(snapshot: Snapshot, part_ids: list[str] | None = None) -> list[Layer]:
    grouped: dict[str, list[FileEntry]] = {}
    for entry in snapshot.state.files:
        part = classify(entry.path)
        grouped.setdefault(part.part_id, []).append(entry)

    layers: list[Layer] = []
    for part_id, files in sorted(grouped.items()):
        if part_ids is not None and part_id not in part_ids:
            continue
        layers.append(
            Layer(
                layer_id=f"{part_id}-{uuid.uuid4().hex[:6]}",
                part_id=part_id,
                title=title_for(part_id),
                source_snapshot=snapshot.snapshot_id,
                desktop=",".join(snapshot.state.desktops),
                desktop_environments=list(snapshot.state.desktop_environments),
                files=list(files),
                # Solo se asocian paquetes observados con una relación conocida
                # o una coincidencia clara con el recurso capturado.
                packages=infer_packages(part_id, list(files), snapshot.state.packages),
            )
        )

    # Las aplicaciones instaladas son su propia capa: se pueden elegir, combinar
    # y compartir igual que el tema o los atajos, y son lo que se instala al
    # aplicar la configuración en otra máquina.
    applications = list(snapshot.state.applications)
    if applications and (part_ids is None or APPLICATIONS_PART.part_id in part_ids):
        layers.append(
            Layer(
                layer_id=f"{APPLICATIONS_PART.part_id}-{uuid.uuid4().hex[:6]}",
                part_id=APPLICATIONS_PART.part_id,
                title=APPLICATIONS_PART.title,
                source_snapshot=snapshot.snapshot_id,
                desktop=",".join(snapshot.state.desktops),
                desktop_environments=list(snapshot.state.desktop_environments),
                files=[],
                packages=[],
                applications=applications,
            )
        )
    return layers


def _layers_dir(root: str) -> Path:
    path = Path(root) / LAYERS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_layer(layer: Layer, root: str = ".", overwrite: bool = True) -> str:
    layer.validate()
    path = safe_record_path(_layers_dir(root), layer.layer_id)
    if path.exists() and not overwrite:
        raise FileExistsError(f"Ya existe una capa con ID {layer.layer_id}.")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(layer.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return str(path)


def load_layer(layer_id: str, root: str = ".") -> Layer:
    path = safe_record_path(Path(root) / LAYERS_DIR, layer_id)
    try:
        return Layer.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise ValueError(f"La capa {layer_id} está dañada.") from exc


def list_layers(root: str = ".") -> list[str]:
    directory = Path(root) / LAYERS_DIR
    if not directory.is_dir():
        return []
    result: list[str] = []
    for path in directory.glob("*.json"):
        try:
            result.append(validate_identifier(path.stem, "ID de capa"))
        except ValidationError:
            continue
    return sorted(result)


def objects_in_use(root: str = ".", exclude_layer: str = "") -> set[str]:
    in_use: set[str] = set()
    for layer_id in list_layers(root):
        if layer_id == exclude_layer:
            continue
        for entry in load_layer(layer_id, root).files:
            if entry.checksum:
                in_use.add(entry.checksum)
    return in_use
