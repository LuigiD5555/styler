"""Lectura, validación y construcción del único formato ``.stylerpkg``."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from styler.automation.specs import default_action_registry
from styler.component_catalog.schema import parse_component

from .models import (
    PACKAGE_SUFFIX,
    ActionDefinition,
    ArtifactEntry,
    GraphDefinition,
    PackageInspection,
    PackageManifest,
    PackageType,
    PortablePackageError,
)

MANIFEST_NAME = "manifest.json"
CHECKSUMS_NAME = "checksums.json"
MAX_FILES = 4096
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024


def _portable_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9._-]+", "-", normalized.lower()).strip("-._")
    return slug or "asset"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "" in path.parts:
        raise PortablePackageError(f"Ruta insegura dentro del paquete: '{name}'.")
    return path


def _validate_archive_info(info: zipfile.ZipInfo) -> None:
    _safe_member(info.filename)
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise PortablePackageError(f"El paquete contiene un enlace simbólico: {info.filename}.")
    if info.file_size > MAX_MEMBER_BYTES:
        raise PortablePackageError(
            f"'{info.filename}' excede el límite de {MAX_MEMBER_BYTES // (1024 * 1024)} MiB."
        )


def _read_json(archive: zipfile.ZipFile, name: str) -> Mapping[str, Any]:
    try:
        raw = archive.read(name)
    except KeyError as exc:
        raise PortablePackageError(f"El paquete no contiene {name}.") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortablePackageError(f"{name} no contiene JSON válido: {exc}") from exc
    if not isinstance(value, Mapping):
        raise PortablePackageError(f"{name} debe contener un objeto JSON.")
    return value


def _version_tuple(value: str) -> tuple[int, ...]:
    numeric = value.split("-", 1)[0].split("+", 1)[0]
    parts: list[int] = []
    for token in numeric.split("."):
        if not token.isdigit():
            raise PortablePackageError(f"Versión no comparable: '{value}'.")
        parts.append(int(token))
    return tuple(parts + [0] * (4 - len(parts)))


def _check_styler_requirement(requirement: str) -> None:
    from styler import __version__

    text = requirement.strip()
    operators = (">=", "<=", "==", ">", "<")
    operator = next((item for item in operators if text.startswith(item)), ">=")
    required_text = text[len(operator):].strip() if text.startswith(operator) else text
    current = _version_tuple(__version__)
    required = _version_tuple(required_text)
    comparisons = {">=": current >= required, "<=": current <= required, "==": current == required,
                   ">": current > required, "<": current < required}
    if not comparisons[operator]:
        raise PortablePackageError(
            f"El paquete requiere Styler {requirement}; esta instalación es {__version__}."
        )


def inspect_package(source: str | Path, *, installed_identities: Iterable[str] = ()) -> PackageInspection:
    path = Path(source)
    if path.suffix != PACKAGE_SUFFIX:
        raise PortablePackageError(f"Se esperaba un archivo {PACKAGE_SUFFIX}: {path}.")
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PortablePackageError(f"No se pudo abrir el paquete: {exc}") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise PortablePackageError(f"El paquete contiene demasiados archivos ({len(infos)}).")
        total = 0
        names: set[str] = set()
        for info in infos:
            _validate_archive_info(info)
            if info.filename in names:
                raise PortablePackageError(f"Entrada duplicada dentro del ZIP: {info.filename}.")
            names.add(info.filename)
            total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise PortablePackageError(
                f"El paquete excede el límite total de {MAX_TOTAL_BYTES // (1024 * 1024)} MiB."
            )
        manifest = PackageManifest.from_dict(_read_json(archive, MANIFEST_NAME))
        _check_styler_requirement(manifest.requires_styler)
        checksums = {str(k): str(v) for k, v in _read_json(archive, CHECKSUMS_NAME).items()}
        expected_paths = {artifact.path for artifact in manifest.artifacts}
        missing = sorted(expected_paths - names)
        if missing:
            raise PortablePackageError("Faltan artefactos declarados: " + ", ".join(missing))
        missing_checksums = sorted(expected_paths - set(checksums))
        if missing_checksums:
            raise PortablePackageError("Faltan sumas de verificación: " + ", ".join(missing_checksums))
        for artifact in manifest.artifacts:
            raw = archive.read(artifact.path)
            if checksums[artifact.path] != _sha256_bytes(raw):
                raise PortablePackageError(
                    f"La suma de '{artifact.path}' no coincide; el paquete fue modificado o está incompleto."
                )
            _validate_artifact(artifact, raw, source=f"{path}!/{artifact.path}")
        identity = f"{manifest.package_id}@{manifest.version}"
        collisions = (identity,) if identity in set(installed_identities) else ()
        warnings = [
            "Las sumas prueban integridad, no identidad del autor; importa solo paquetes de una fuente reconocida."
        ]
        if manifest.package_type is PackageType.CHANGE:
            warnings.append(
                "El paquete contiene una receta y un grafo declarativos. Importarlo no modifica el sistema."
            )
        else:
            warnings.append("El paquete contiene una línea base; importarlo solo registra un punto de comparación.")
        target = manifest.metadata.get("target_baseline")
        if isinstance(target, Mapping):
            name = str(target.get("name") or target.get("baseline_id") or "declarada")
            warnings.append(f"El cambio fue construido contra la línea base '{name}'.")
        return PackageInspection(manifest=manifest, source=str(path), checksum_verified=True,
                                 warnings=tuple(warnings), collisions=collisions,
                                 total_files=len(infos), total_bytes=total)


def _validate_artifact(entry: ArtifactEntry, raw: bytes, *, source: str) -> None:
    if entry.kind == "asset":
        return
    if entry.kind == "component":
        try:
            import tomllib
            data = tomllib.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise PortablePackageError(f"Componente inválido en {source}: {exc}") from exc
        definition = parse_component(data, source)
        if definition.id != entry.artifact_id:
            raise PortablePackageError(
                f"El manifiesto declara '{entry.artifact_id}' pero el componente define '{definition.id}'."
            )
        return
    if entry.kind == "recipe":
        from styler.change_recipe import loads_recipe
        recipe = loads_recipe(raw.decode("utf-8"))
        if recipe.recipe_id != entry.artifact_id:
            raise PortablePackageError(
                f"El manifiesto declara '{entry.artifact_id}' pero la receta define '{recipe.recipe_id}'."
            )
        return
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortablePackageError(f"Artefacto JSON inválido en {source}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise PortablePackageError(f"El artefacto {source} debe ser un objeto.")
    if entry.kind == "baseline":
        from styler.baselines.models import BaselineDefinition
        baseline = BaselineDefinition.from_dict(data)
        if baseline.baseline_id != entry.artifact_id:
            raise PortablePackageError(
                f"El manifiesto declara '{entry.artifact_id}' pero la línea base define '{baseline.baseline_id}'."
            )
    elif entry.kind == "action":
        action = ActionDefinition.from_dict(data)
        if action.action_id != entry.artifact_id:
            raise PortablePackageError(
                f"El manifiesto declara '{entry.artifact_id}' pero la acción define '{action.action_id}'."
            )
        action.validate(default_action_registry())
    elif entry.kind == "graph":
        graph = GraphDefinition.from_dict(data)
        if graph.graph_id != entry.artifact_id:
            raise PortablePackageError(
                f"El manifiesto declara '{entry.artifact_id}' pero el grafo define '{graph.graph_id}'."
            )
    else:
        raise PortablePackageError(f"Tipo de artefacto no validable: {entry.kind}")


def build_package(manifest: PackageManifest, artifact_contents: Mapping[str, bytes], destination: str | Path) -> Path:
    destination = Path(destination)
    if destination.suffix != PACKAGE_SUFFIX:
        destination = destination.with_suffix(PACKAGE_SUFFIX)
    declared = {artifact.path for artifact in manifest.artifacts}
    supplied = set(artifact_contents)
    if declared != supplied:
        missing = declared - supplied
        extra = supplied - declared
        details = []
        if missing:
            details.append("faltan: " + ", ".join(sorted(missing)))
        if extra:
            details.append("sobran: " + ", ".join(sorted(extra)))
        raise PortablePackageError("Los artefactos no coinciden con el manifiesto (" + "; ".join(details) + ").")
    for artifact in manifest.artifacts:
        _validate_artifact(artifact, artifact_contents[artifact.path], source=artifact.path)
    checksums = {path: _sha256_bytes(content) for path, content in sorted(artifact_contents.items())}
    manifest_raw = json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")
    checksums_raw = json.dumps(checksums, indent=2, ensure_ascii=False).encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="stylerpkg-", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temporary = Path(temp_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST_NAME, manifest_raw)
            archive.writestr(CHECKSUMS_NAME, checksums_raw)
            for path, content in sorted(artifact_contents.items()):
                archive.writestr(path, content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def read_artifact(source: str | Path, entry: ArtifactEntry) -> bytes:
    inspection = inspect_package(source)
    match = next((item for item in inspection.manifest.artifacts if item == entry), None)
    if match is None:
        raise PortablePackageError(f"El artefacto {entry.kind}:{entry.artifact_id} no pertenece al paquete.")
    with zipfile.ZipFile(source) as archive:
        return archive.read(entry.path)


def artifact_from_file(kind: str, source: str | Path) -> tuple[ArtifactEntry, bytes]:
    path = Path(source)
    raw = path.read_bytes()
    if kind == "recipe":
        from styler.change_recipe import loads_recipe
        recipe = loads_recipe(raw.decode("utf-8"))
        artifact_id, title, relative = recipe.recipe_id, recipe.name, f"recipe/{recipe.recipe_id}.yaml"
    elif kind == "baseline":
        from styler.baselines.models import BaselineDefinition
        baseline = BaselineDefinition.from_dict(json.loads(raw.decode("utf-8")))
        artifact_id, title, relative = baseline.baseline_id, baseline.name, f"baseline/{baseline.baseline_id}.json"
    elif kind == "action":
        action = ActionDefinition.from_dict(json.loads(raw.decode("utf-8")))
        artifact_id, title, relative = action.action_id, action.title, f"actions/{action.action_id}.json"
    elif kind == "graph":
        graph = GraphDefinition.from_dict(json.loads(raw.decode("utf-8")))
        artifact_id, title, relative = graph.graph_id, graph.title, f"graph/{graph.graph_id}.json"
    elif kind == "component":
        import tomllib
        definition = parse_component(tomllib.loads(raw.decode("utf-8")), str(path))
        artifact_id, title, relative = definition.id, definition.name, f"components/{definition.id}.toml"
    elif kind == "asset":
        artifact_id, title, relative = _portable_slug(path.name), path.name, f"assets/{_portable_slug(path.name)}"
    else:
        raise PortablePackageError(f"Tipo de artefacto no soportado: {kind}")
    entry = ArtifactEntry(kind, artifact_id, relative, title=title)
    _validate_artifact(entry, raw, source=str(path))
    return entry, raw
