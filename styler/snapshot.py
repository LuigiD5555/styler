"""Configuraciones guardadas de las rutas administradas por Styler.

Una configuración guardada es una referencia a archivos materializados en el
ObjectStore. Aplicarla significa volver a escribir esas rutas; no significa
hacer idéntico todo el HOME ni revertir paquetes o servicios.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from styler.capture import capture_state
from styler.models import Changeset, Component, Decision, FileEntry, State
from styler.objectstore import ObjectStore, ObjectStoreError
from styler.runtime.builder import workflow_from_changeset
from styler.runtime.engine import WorkflowEngine
from styler.runtime.models import ExecutionContext, WorkflowRun
from styler.validation import (
    ValidationError,
    safe_record_path,
    validate_checksum,
    validate_identifier,
    validate_logical_path,
)

STYLER_DIR = ".styler"
SNAPSHOTS_DIR = os.path.join(STYLER_DIR, "snapshots")
VALID_ORIGINS = {"manual", "automatic", "imported", "backup"}


@dataclass
class Snapshot:
    snapshot_id: str
    label: str
    created_at: float = field(default_factory=time.time)
    origin: str = "manual"
    parent_id: str = ""
    state: State = None  # type: ignore[assignment]

    def to_dict(self) -> dict:
        return {
            "schema_version": 2,
            "snapshot_id": self.snapshot_id,
            "label": self.label,
            "created_at": self.created_at,
            "origin": self.origin,
            "parent_id": self.parent_id,
            "state": self.state.to_dict(),
        }

    @staticmethod
    def from_dict(d: dict) -> "Snapshot":
        snapshot = Snapshot(
            snapshot_id=d["snapshot_id"],
            label=d.get("label", ""),
            created_at=d.get("created_at", time.time()),
            origin=d.get("origin", "manual"),
            parent_id=d.get("parent_id", ""),
            state=State.from_dict(d["state"]),
        )
        validate_identifier(snapshot.snapshot_id, "ID de configuración")
        if snapshot.origin not in VALID_ORIGINS:
            raise ValidationError("Origen de configuración inválido.")
        return snapshot

    def restorable_files(self) -> list[FileEntry]:
        result: list[FileEntry] = []
        for entry in self.state.files:
            try:
                validate_logical_path(entry.path)
                validate_checksum(entry.checksum)
                result.append(entry)
            except ValidationError:
                # Compatibilidad visual con datos v0.3 que todavía tenían
                # object_path; su aplicación seguirá pasando por preflight.
                if entry.object_path:
                    result.append(entry)
        return result


def _to_real_path(portable_path: str) -> Path:
    if portable_path == "${HOME}":
        return Path.home()
    if portable_path.startswith("${HOME}/"):
        return Path.home() / portable_path[len("${HOME}/"):]
    return Path(portable_path)


def materialize_files(files: list[FileEntry], store: ObjectStore) -> list[FileEntry]:
    materialized: list[FileEntry] = []
    for entry in files:
        try:
            validate_logical_path(entry.path)
        except ValidationError:
            # Una captura puede observar rutas fuera del espacio administrado; se
            # conservan como metadatos, pero no se materializan automáticamente.
            materialized.append(entry)
            continue
        real_path = _to_real_path(entry.path)
        checksum = entry.checksum
        object_path = ""
        size = entry.size
        mode = entry.mode
        if real_path.is_file() and not real_path.is_symlink():
            try:
                checksum, object_path = store.store_file(real_path)
                stat = real_path.stat()
                size = stat.st_size
                mode = oct(stat.st_mode & 0o777)
            except (OSError, ObjectStoreError):
                object_path = ""
        materialized.append(
            FileEntry(
                path=entry.path,
                checksum=checksum,
                size=size,
                mode=mode,
                owner_hint=entry.owner_hint,
                object_path=object_path,
            )
        )
    return materialized


def _snapshots_dir(root: str) -> Path:
    path = Path(root) / SNAPSHOTS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_snapshot(
    label: str,
    root: str = ".",
    origin: str = "manual",
    parent_id: str = "",
    observers: list | None = None,
    scope: str = "plasma",
) -> Snapshot:
    if origin not in VALID_ORIGINS:
        raise ValueError(f"origin inválido: {origin!r}")
    state = capture_state(label, observers=observers, scope=scope, root=root)
    store = ObjectStore(root=root)
    state.files = materialize_files(state.files, store)
    snapshot = Snapshot(
        snapshot_id=state.state_id,
        label=label,
        origin=origin,
        parent_id=parent_id,
        state=state,
    )
    save_snapshot(snapshot, root=root)
    return snapshot


def save_snapshot(snapshot: Snapshot, root: str = ".") -> str:
    validate_identifier(snapshot.snapshot_id, "ID de configuración")
    path = safe_record_path(_snapshots_dir(root), snapshot.snapshot_id)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return str(path)


def load_snapshot(snapshot_id: str, root: str = ".") -> Snapshot:
    path = safe_record_path(Path(root) / SNAPSHOTS_DIR, snapshot_id)
    try:
        return Snapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise ValueError(f"La configuración {snapshot_id} está dañada.") from exc


def list_snapshots(root: str = ".") -> list[str]:
    directory = Path(root) / SNAPSHOTS_DIR
    if not directory.is_dir():
        return []
    result: list[str] = []
    for path in directory.glob("*.json"):
        try:
            result.append(validate_identifier(path.stem, "ID de configuración"))
        except ValidationError:
            continue
    return sorted(result)


def _restore_changeset(snapshot: Snapshot) -> Changeset:
    """Genera una aplicación de archivos únicamente.

    Paquetes y servicios permanecen como información del snapshot, pero no se
    ejecutan automáticamente ni se prometen como parte de la restauración.
    """
    component = Component(
        component_id="aplicar-configuracion",
        title=snapshot.label,
        category="configuracion",
        files=list(snapshot.state.files),
        packages=[],
        services=[],
        decision=Decision.INCLUDE,
        human_summary=f"Aplicar la configuración «{snapshot.label}».",
    )
    return Changeset(
        changeset_id=f"apply-{snapshot.snapshot_id}",
        base_state="",
        target_state=snapshot.snapshot_id,
        components=[component],
    )


def restore_snapshot(
    snapshot_id: str,
    root: str = ".",
    execute: bool = False,
    approve: bool = False,
    run_id: str | None = None,
) -> WorkflowRun:
    """Compatibilidad CLI: aplica los archivos guardados mediante el motor.

    Las aplicaciones de producto deben preferir ``transaction.py``, que añade
    journal y rollback verificable.
    """
    snapshot = load_snapshot(snapshot_id, root=root)
    workflow = workflow_from_changeset(_restore_changeset(snapshot), name=f"apply-{snapshot_id}")
    context = ExecutionContext(
        root=Path(root),
        dry_run=not execute,
        approve=approve,
        run_id=run_id or "",
    )
    return WorkflowEngine().run(workflow, context)
