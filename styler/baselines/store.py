"""Persistencia local y transporte de líneas base mediante ``.stylerpkg``."""
from __future__ import annotations

import json
from pathlib import Path

from styler.portable import (
    ArtifactEntry, PackageManifest, PackageType, PortablePackageError,
    build_package, inspect_package, read_artifact,
)
from styler.provenance.models import SystemIdentity
from .models import BaselineDefinition, BaselineError, BaselineKind

BASELINES_DIR = "baselines"
ACTIVE_POINTER = "active.json"
INTERNAL_JSON_FILES = {ACTIVE_POINTER, "bundled-catalog.json"}


def baselines_dir(root: str | Path = ".") -> Path:
    path = Path(root) / ".styler" / BASELINES_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def save(definition: BaselineDefinition, root: str | Path = ".") -> str:
    target = baselines_dir(root) / f"{definition.baseline_id}.json"
    _atomic_json(target, definition.to_dict())
    return str(target)


def load(baseline_id: str, root: str | Path = ".") -> BaselineDefinition:
    path = baselines_dir(root) / f"{baseline_id}.json"
    if not path.is_file():
        raise BaselineError(f"No existe la línea base: {baseline_id}")
    try:
        return BaselineDefinition.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise BaselineError(f"No se pudo leer la línea base {baseline_id}: {exc}") from exc


def list_all(root: str | Path = ".") -> list[BaselineDefinition]:
    definitions: list[BaselineDefinition] = []
    for path in baselines_dir(root).glob("*.json"):
        if path.name in INTERNAL_JSON_FILES or path.name.endswith(".tmp"):
            continue
        try:
            definitions.append(BaselineDefinition.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError, KeyError, BaselineError):
            continue
    return sorted(definitions, key=lambda item: (item.kind is not BaselineKind.OFFICIAL, -item.created_at, item.name.lower()))


def broken_entries(root: str | Path = ".") -> dict[str, str]:
    broken: dict[str, str] = {}
    for path in baselines_dir(root).glob("*.json"):
        if path.name in INTERNAL_JSON_FILES or path.name.endswith(".tmp"):
            continue
        try:
            BaselineDefinition.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, KeyError, BaselineError) as exc:
            broken[path.stem] = str(exc)
    return broken


def activate(baseline_id: str, root: str | Path = ".") -> BaselineDefinition:
    definition = load(baseline_id, root=root)
    _atomic_json(baselines_dir(root) / ACTIVE_POINTER, {"baseline_id": definition.baseline_id})
    return definition


def deactivate(root: str | Path = ".") -> None:
    (baselines_dir(root) / ACTIVE_POINTER).unlink(missing_ok=True)


def active(root: str | Path = ".") -> BaselineDefinition | None:
    pointer = baselines_dir(root) / ACTIVE_POINTER
    if not pointer.is_file():
        return None
    try:
        baseline_id = json.loads(pointer.read_text(encoding="utf-8"))["baseline_id"]
        return load(str(baseline_id), root=root)
    except (OSError, ValueError, TypeError, KeyError, BaselineError):
        return None


def recommended(system: SystemIdentity, root: str | Path = ".") -> BaselineDefinition | None:
    candidates: list[tuple[int, float, BaselineDefinition]] = []
    for definition in list_all(root):
        if not definition.is_official:
            continue
        score = definition.compatibility_score(system)
        if score >= 0:
            candidates.append((score, definition.created_at, definition))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def export_definition_package(definition: BaselineDefinition, destination: str | Path) -> Path:
    """Empaqueta una definición concreta sin registrarla en la biblioteca local."""
    entry = ArtifactEntry(
        "baseline",
        definition.baseline_id,
        f"baseline/{definition.baseline_id}.json",
        title=definition.name,
    )
    manifest = PackageManifest(
        package_id=f"baseline.{definition.baseline_id}",
        name=definition.name,
        version=definition.version,
        package_type=PackageType.BASELINE,
        description=definition.description,
        author=definition.author,
        metadata={"baseline_id": definition.baseline_id},
        artifacts=(entry,),
    )
    raw = json.dumps(definition.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")
    try:
        return build_package(manifest, {entry.path: raw}, destination)
    except PortablePackageError as exc:
        raise BaselineError(str(exc)) from exc


def export_package(baseline_id: str, destination: str | Path, root: str | Path = ".") -> Path:
    return export_definition_package(load(baseline_id, root=root), destination)


def import_package(
    source: str | Path, root: str | Path = ".", *, trust: bool = False,
    activate_after: bool = False, source_label: str = "",
) -> BaselineDefinition:
    try:
        inspection = inspect_package(source)
    except PortablePackageError as exc:
        raise BaselineError(str(exc)) from exc
    if inspection.manifest.package_type is not PackageType.BASELINE:
        raise BaselineError("El .stylerpkg contiene un cambio, no una línea base.")
    entry = inspection.manifest.artifacts[0]
    try:
        definition = BaselineDefinition.from_dict(json.loads(read_artifact(source, entry).decode("utf-8")))
    except (PortablePackageError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        raise BaselineError(f"No se pudo leer la línea base del paquete: {exc}") from exc
    payload = definition.to_dict()
    payload["trusted"] = bool(trust)
    payload["source"] = source_label or str(source)
    definition = BaselineDefinition.from_dict(payload)
    save(definition, root=root)
    if activate_after:
        activate(definition.baseline_id, root=root)
    return definition



def remove_retired_bundled(baseline_id: str, root: str | Path = ".") -> bool:
    """Retira una baseline oficial que ya no pertenece al catálogo empacado.

    No es una acción de usuario. Solo ``BaselineService.sync_bundled`` la usa
    al actualizar Styler, para que el catálogo local refleje exactamente el
    catálogo de esta versión y no acumule defaults oficiales antiguos.
    """
    try:
        definition = load(baseline_id, root=root)
    except BaselineError:
        return False
    if not (definition.is_official and definition.source.startswith("bundled-catalog:")):
        return False
    current = active(root=root)
    if current and current.baseline_id == baseline_id:
        deactivate(root=root)
    (baselines_dir(root) / f"{baseline_id}.json").unlink(missing_ok=True)
    return True

def remove(baseline_id: str, root: str | Path = ".") -> None:
    definition = load(baseline_id, root=root)
    if definition.is_official:
        raise BaselineError("Las líneas base oficiales no se eliminan; pueden repararse desde el catálogo.")
    current = active(root=root)
    if current and current.baseline_id == baseline_id:
        deactivate(root=root)
    (baselines_dir(root) / f"{baseline_id}.json").unlink()
