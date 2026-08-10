"""Convierte cambios detectados en una receta revisable y assets empaquetados."""
from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

from styler.provenance.classification import can_generate_install
from styler.provenance.models import ApplicationRecord, ArtifactKind, SystemArtifactRecord
from .models import ChangeRecipe, RecipeOperation

SUPPORTED_PACKAGE_MANAGERS = frozenset({"apt", "flatpak", "pacman", "aur", "rpm", "zypper", "snap"})


@dataclass(frozen=True)
class AssetPayload:
    path: str
    content: bytes
    mode: int


@dataclass(frozen=True)
class SynthesisResult:
    recipe: ChangeRecipe
    assets: tuple[AssetPayload, ...] = ()
    included_ids: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-._") or "item"


def _expand_portable(path: str, home: Path) -> Path:
    if path == "${HOME}":
        return home
    if path.startswith("${HOME}/"):
        return home / path[len("${HOME}/"):]
    return Path(path)


def _bundle(source: Path, prefix: str) -> list[AssetPayload]:
    if source.is_symlink():
        raise ValueError("no se empaquetan enlaces simbólicos")
    if source.is_file():
        return [
            AssetPayload(
                f"{prefix}/{source.name}",
                source.read_bytes(),
                stat.S_IMODE(source.stat().st_mode),
            )
        ]
    payloads: list[AssetPayload] = []
    for item in sorted(source.rglob("*"), key=lambda path: path.as_posix()):
        if item.is_symlink() or not item.is_file():
            continue
        payloads.append(
            AssetPayload(
                f"{prefix}/{source.name}/{item.relative_to(source).as_posix()}",
                item.read_bytes(),
                stat.S_IMODE(item.stat().st_mode),
            )
        )
    return payloads


def _package_config(record: ApplicationRecord) -> dict[str, str]:
    """Conserva la procedencia que el ejecutor necesita para reproducir la instalación."""

    config = {"manager": record.manager, "name": record.name}
    for key in ("remote_name", "remote_url", "branch", "ref", "channel", "commit"):
        value = str(getattr(record.origin, key, "") or "").strip()
        if value:
            config[key] = value
    return config


def _setting_target_kinds(record: SystemArtifactRecord) -> tuple[ArtifactKind, ...]:
    text = " ".join(
        [record.setting_schema, record.setting_group, record.setting_key, record.name]
    ).lower()
    if "cursor" in text:
        return (ArtifactKind.CURSOR_THEME,)
    if "icon" in text:
        return (ArtifactKind.ICON_THEME,)
    if "picture" in text or "wallpaper" in text or "fondo" in text:
        return (ArtifactKind.WALLPAPER,)
    if "theme" in text or "colorscheme" in text or "widgetstyle" in text or "tema" in text:
        return (ArtifactKind.THEME,)
    return ()


def _normalized_setting_value(value: str) -> str:
    text = value.strip().strip("'\"")
    if text.startswith("file://"):
        parsed = urlparse(text)
        text = unquote(parsed.path)
    return text.lower()


def _setting_dependencies(
    setting: SystemArtifactRecord,
    asset_operations: list[tuple[SystemArtifactRecord, str]],
) -> tuple[str, ...]:
    expected = set(_setting_target_kinds(setting))
    if not expected:
        return ()
    candidates = [(record, operation_id) for record, operation_id in asset_operations if record.kind in expected]
    if not candidates:
        return ()
    desired = _normalized_setting_value(setting.setting_value)
    matched: list[str] = []
    for record, operation_id in candidates:
        names = {
            record.name.lower(),
            Path(record.path).name.lower(),
            Path(record.path).stem.lower(),
        }
        if any(name and (desired == name or name in desired) for name in names):
            matched.append(operation_id)
    if matched:
        return tuple(dict.fromkeys(matched))
    # Si la selección contiene un único recurso del tipo que activa este ajuste,
    # es la relación semántica más fuerte disponible y se hace visible en el DAG.
    if len(candidates) == 1:
        return (candidates[0][1],)
    return ()


def synthesize_recipe(
    recipe_id: str,
    name: str,
    applications: Iterable[ApplicationRecord],
    artifacts: Iterable[SystemArtifactRecord],
    *,
    baseline_id: str = "",
    description: str = "",
    home: str | Path | None = None,
) -> SynthesisResult:
    home_path = Path(home or Path.home()).expanduser().resolve()
    application_records = list(applications)
    artifact_records = list(artifacts)
    operations: list[RecipeOperation] = []
    assets: list[AssetPayload] = []
    included: list[str] = []
    skipped: list[tuple[str, str]] = []
    warnings: list[str] = []

    for record in application_records:
        operation_id = _slug(record.app_id)
        supported, support_reason = can_generate_install(record)
        if not supported:
            skipped.append((record.app_id, support_reason))
            continue
        if record.manager in SUPPORTED_PACKAGE_MANAGERS:
            config = _package_config(record)
            operations.append(
                RecipeOperation(
                    operation_id=operation_id,
                    kind="package.install",
                    title=f"Instalar {record.display_name or record.name}",
                    config=config,
                    provides=(f"application.{_slug(record.name)}.installed",),
                    verification={"manager": record.manager, "name": record.name},
                )
            )
            included.append(record.app_id)
        elif record.manager == "appimage" and record.integrity.artifact_path:
            source = Path(record.integrity.artifact_path).expanduser()
            if not source.is_file():
                skipped.append((record.app_id, "El archivo AppImage ya no existe."))
                continue
            prefix = f"assets/{operation_id}"
            assets.extend(_bundle(source, prefix))
            target = "${HOME}/Applications"
            operations.append(
                RecipeOperation(
                    operation_id=operation_id,
                    kind="asset.overlay",
                    title=f"Instalar {record.display_name or record.name} AppImage",
                    config={
                        "source": f"package://{prefix}",
                        "target": target,
                        "modes": {source.name: stat.S_IMODE(source.stat().st_mode) | 0o100},
                    },
                    verification={
                        "path": f"{target}/{source.name}",
                        "checksum": record.integrity.checksum,
                    },
                )
            )
            included.append(record.app_id)
        else:
            skipped.append(
                (record.app_id, f"El gestor '{record.manager}' todavía no tiene síntesis ejecutable.")
            )

    asset_operations: list[tuple[SystemArtifactRecord, str]] = []
    settings: list[SystemArtifactRecord] = []
    for record in artifact_records:
        if record.scope != "user":
            skipped.append(
                (
                    record.artifact_id,
                    "Los recursos del sistema requieren una política privilegiada explícita.",
                )
            )
            continue
        if record.kind is ArtifactKind.SETTING:
            settings.append(record)
            continue
        operation_id = _slug(record.artifact_id)
        source = _expand_portable(record.path, home_path)
        if not source.exists():
            skipped.append((record.artifact_id, "El recurso ya no existe."))
            continue
        prefix = f"assets/{operation_id}"
        payload = _bundle(source, prefix)
        if not payload:
            skipped.append((record.artifact_id, "El recurso no contiene archivos."))
            continue
        assets.extend(payload)
        target = record.path.rsplit("/", 1)[0] if "/" in record.path else "${HOME}"
        modes = {source.name: record.mode} if source.is_file() else {}
        operations.append(
            RecipeOperation(
                operation_id=operation_id,
                kind="asset.overlay",
                title=f"Aplicar {record.kind.human}: {record.name}",
                config={"source": f"package://{prefix}", "target": target, "modes": modes},
                verification={"path": record.path, "checksum": record.checksum},
            )
        )
        asset_operations.append((record, operation_id))
        included.append(record.artifact_id)

    for record in settings:
        if not record.setting_backend or not record.setting_key or not record.setting_value:
            skipped.append(
                (
                    record.artifact_id,
                    "El ajuste visual no contiene backend, clave y valor suficientes.",
                )
            )
            continue
        operation_id = _slug(record.artifact_id)
        config = {
            "backend": record.setting_backend,
            "schema": record.setting_schema,
            "group": record.setting_group,
            "key": record.setting_key,
            "value": record.setting_value,
        }
        operations.append(
            RecipeOperation(
                operation_id=operation_id,
                kind="setting.apply",
                title=f"Aplicar {record.kind.human}: {record.name}",
                config=config,
                needs=_setting_dependencies(record, asset_operations),
                verification=dict(config),
                provides=(f"setting.{operation_id}.applied",),
            )
        )
        included.append(record.artifact_id)

    if not operations:
        raise ValueError("La selección no contiene cambios que Styler pueda convertir en una receta.")
    recipe = ChangeRecipe(
        recipe_id=_slug(recipe_id),
        name=name,
        description=description,
        baseline_id=baseline_id,
        operations=tuple(operations),
        warnings=tuple(warnings),
    )
    return SynthesisResult(
        recipe=recipe,
        assets=tuple(assets),
        included_ids=tuple(included),
        skipped=tuple(skipped),
        warnings=tuple(warnings),
    )
