"""Aplicación transaccional de archivos administrados por Styler.

La unidad reversible es un conjunto explícito de rutas dentro de HOME. Antes de
escribir se valida todo el plan, se captura el estado previo de cada ruta y se
persiste un journal. Paquetes y servicios no forman parte de esta transacción.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from styler.hashing import hash_file
from styler.models import FileEntry, State
from styler.objectstore import ObjectStore, ObjectStoreError
from styler.planning.models import Status, StepResult, WorkflowRun
from styler import snapshot as snapshot_mod
from styler.snapshot import Snapshot, load_snapshot, save_snapshot
from styler.validation import (
    ValidationError,
    resolve_home_path,
    safe_record_path,
    validate_checksum,
    validate_identifier,
    validate_logical_path,
)

STYLER_DIR = ".styler"
TRANSACTIONS_DIR = os.path.join(STYLER_DIR, "transactions")
JOURNALS_DIR = os.path.join(STYLER_DIR, "journals")
RUNS_DIR = os.path.join(STYLER_DIR, "runs")


class RollbackStatus:
    NOT_NEEDED = "not_needed"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass
class JournalEntry:
    logical_path: str
    target_path: str
    desired_checksum: str
    desired_mode: str = ""
    previous_type: str = "missing"  # missing | file
    previous_checksum: str = ""
    previous_mode: str = ""
    created_directories: list[str] = field(default_factory=list)
    applied: bool = False
    rolled_back: bool = False
    rollback_error: str = ""


@dataclass
class Journal:
    transaction_id: str
    source_type: str
    source_id: str
    home: str
    created_at: float
    entries: list[JournalEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "transaction_id": self.transaction_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "home": self.home,
            "created_at": self.created_at,
            "entries": [asdict(entry) for entry in self.entries],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Journal":
        return Journal(
            transaction_id=data["transaction_id"],
            source_type=data.get("source_type", "snapshot"),
            source_id=data.get("source_id", ""),
            home=data["home"],
            created_at=float(data.get("created_at", 0)),
            entries=[JournalEntry(**entry) for entry in data.get("entries", [])],
        )


@dataclass
class TransactionRecord:
    transaction_id: str
    target_snapshot: str = ""  # compatibilidad v0.3
    backup_snapshot: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    applied: bool = False
    rolled_back: bool = False
    run_id: str = ""
    rollback_run_id: str = ""
    error: str = ""
    source_type: str = "snapshot"
    source_id: str = ""
    journal_path: str = ""
    rollback_status: str = RollbackStatus.NOT_NEEDED
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _directory(root: str, relative: str) -> Path:
    path = Path(root) / relative
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_record(record: TransactionRecord, root: str) -> str:
    path = safe_record_path(_directory(root, TRANSACTIONS_DIR), record.transaction_id)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return str(path)


def load_transaction(transaction_id: str, root: str = ".") -> TransactionRecord:
    path = safe_record_path(Path(root) / TRANSACTIONS_DIR, transaction_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    # Compatibilidad con registros v0.3 sin campos nuevos.
    defaults = TransactionRecord(transaction_id=transaction_id).to_dict()
    defaults.update(data)
    return TransactionRecord(**defaults)


def list_transactions(root: str = ".") -> list[str]:
    directory = Path(root) / TRANSACTIONS_DIR
    if not directory.is_dir():
        return []
    result: list[str] = []
    for path in directory.glob("*.json"):
        try:
            result.append(validate_identifier(path.stem, "ID de transacción"))
        except ValidationError:
            continue
    return sorted(result)


def _save_journal(journal: Journal, root: str) -> str:
    path = safe_record_path(_directory(root, JOURNALS_DIR), journal.transaction_id)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(journal.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return str(path)


def load_journal(transaction_id: str, root: str = ".") -> Journal:
    path = safe_record_path(Path(root) / JOURNALS_DIR, transaction_id)
    return Journal.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _parse_mode(value: str) -> int | None:
    if not value:
        return None
    text = str(value).strip().lower()
    try:
        mode = int(text, 8)
    except ValueError as exc:
        raise ValidationError(f"Permisos inválidos: {value}") from exc
    if mode < 0 or mode > 0o7777:
        raise ValidationError(f"Permisos fuera de rango: {value}")
    return mode


def _created_parent_directories(target: Path, home: Path) -> list[str]:
    missing: list[Path] = []
    current = target.parent
    while current != home and not current.exists():
        missing.append(current)
        current = current.parent
    return [str(path) for path in reversed(missing)]


def _validate_entries(entries: list[FileEntry], root: str, home: Path) -> list[tuple[FileEntry, Path]]:
    store = ObjectStore(root=root)
    validated: list[tuple[FileEntry, Path]] = []
    seen: set[str] = set()
    for entry in entries:
        logical = validate_logical_path(entry.path)
        checksum = validate_checksum(entry.checksum)
        if logical in seen:
            raise ValidationError(f"El plan contiene dos operaciones para la misma ruta: {logical}")
        seen.add(logical)
        destination = resolve_home_path(logical, home)
        if destination == home:
            raise ValidationError("No se puede reemplazar el directorio HOME completo.")
        if destination.exists() and (destination.is_symlink() or destination.is_dir()):
            raise ValidationError(f"El destino no es un archivo regular: {logical}")
        _parse_mode(entry.mode)
        if not store.verify(checksum):
            raise ObjectStoreError(f"Objeto ausente o corrupto para {logical}: {checksum}")
        validated.append((entry, destination))
    return validated


def _capture_journal(
    transaction_id: str,
    source_type: str,
    source_id: str,
    validated: list[tuple[FileEntry, Path]],
    root: str,
    home: Path,
) -> Journal:
    store = ObjectStore(root=root)
    journal = Journal(
        transaction_id=transaction_id,
        source_type=source_type,
        source_id=source_id,
        home=str(home),
        created_at=time.time(),
    )
    for entry, target in validated:
        previous_type = "missing"
        previous_checksum = ""
        previous_mode = ""
        if target.is_file():
            previous_type = "file"
            previous_checksum, _ = store.store_file(target)
            previous_mode = oct(target.stat().st_mode & 0o7777)
        journal.entries.append(
            JournalEntry(
                logical_path=entry.path,
                target_path=str(target),
                desired_checksum=entry.checksum,
                desired_mode=entry.mode,
                previous_type=previous_type,
                previous_checksum=previous_checksum,
                previous_mode=previous_mode,
                created_directories=_created_parent_directories(target, home),
            )
        )
    return journal


def _backup_snapshot_from_journal(journal: Journal, label: str, root: str) -> str:
    files = [
        FileEntry(
            path=entry.logical_path,
            checksum=entry.previous_checksum,
            mode=entry.previous_mode,
            object_path=str(ObjectStore(root=root).path_for(entry.previous_checksum)),
        )
        for entry in journal.entries
        if entry.previous_type == "file" and entry.previous_checksum
    ]
    backup_id = f"backup-{uuid.uuid4().hex[:10]}"
    snapshot = Snapshot(
        snapshot_id=backup_id,
        label=label,
        origin="backup",
        state=State(state_id=backup_id, label=label, files=files),
    )
    save_snapshot(snapshot, root=root)
    return backup_id


def _rollback(journal: Journal, root: str) -> tuple[str, list[str]]:
    store = ObjectStore(root=root)
    errors: list[str] = []
    for entry in reversed(journal.entries):
        if not entry.applied:
            continue
        target = Path(entry.target_path)
        try:
            if entry.previous_type == "missing":
                if target.exists():
                    if not target.is_file() or target.is_symlink():
                        raise OSError("la ruta creada dejó de ser un archivo regular")
                    target.unlink()
                for directory in reversed(entry.created_directories):
                    path = Path(directory)
                    if path.is_dir():
                        try:
                            path.rmdir()
                        except OSError:
                            break
            else:
                store.restore_file(
                    entry.previous_checksum,
                    target,
                    mode=_parse_mode(entry.previous_mode),
                )
            entry.rolled_back = True
        except (OSError, ObjectStoreError, ValidationError) as exc:
            entry.rollback_error = str(exc)
            errors.append(f"{entry.logical_path}: {exc}")

    # Verificación posterior ruta por ruta.
    for entry in journal.entries:
        if not entry.applied:
            continue
        target = Path(entry.target_path)
        try:
            if entry.previous_type == "missing":
                if target.exists():
                    raise OSError("la ruta no fue eliminada")
            else:
                if not target.is_file():
                    raise OSError("el archivo previo no existe")
                actual, _ = hash_file(str(target))
                if actual != entry.previous_checksum:
                    raise OSError("el contenido previo no fue restaurado")
        except OSError as exc:
            message = f"{entry.logical_path}: {exc}"
            if message not in errors:
                errors.append(message)

    if not errors:
        return RollbackStatus.COMPLETED, []
    completed = sum(1 for entry in journal.entries if entry.applied and entry.rolled_back)
    return (RollbackStatus.PARTIAL if completed else RollbackStatus.FAILED), errors


def _make_run(
    run_id: str,
    workflow: str,
    dry_run: bool,
    results: list[StepResult],
    root: str,
    started: datetime,
) -> WorkflowRun:
    finished = datetime.now(timezone.utc)
    run_dir = _directory(root, os.path.join(RUNS_DIR, run_id))
    report = run_dir / "report.json"
    success = all(result.success for result in results)
    run = WorkflowRun(
        run_id=run_id,
        workflow=workflow,
        success=success,
        dry_run=dry_run,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        results=results,
        order=[result.step_id for result in results],
        run_dir=str(run_dir),
        artifacts_dir=str(run_dir / "artifacts"),
        logs_dir=str(run_dir / "logs"),
        report_path=str(report),
    )
    report.write_text(json.dumps(run.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return run


def apply_entries_transactional(
    entries: list[FileEntry],
    source_type: str,
    source_id: str,
    root: str = ".",
    execute: bool = False,
    approve: bool = False,
    label: str = "Configuración",
    home: str | Path | None = None,
    _fault_after: int | None = None,
) -> tuple[WorkflowRun, TransactionRecord]:
    validate_identifier(source_id, "ID de origen")
    transaction_id = uuid.uuid4().hex[:12]
    run_id = f"apply-{transaction_id}"
    started_dt = datetime.now(timezone.utc)
    record = TransactionRecord(
        transaction_id=transaction_id,
        target_snapshot=source_id if source_type == "snapshot" else "",
        started_at=time.time(),
        source_type=source_type,
        source_id=source_id,
        run_id=run_id,
    )
    home_path = Path(home) if home is not None else snapshot_mod.Path.home()
    home_path = home_path.resolve()

    try:
        validated = _validate_entries(entries, root=root, home=home_path)
    except (ValidationError, ObjectStoreError) as exc:
        result = StepResult(
            step_id="preflight",
            step_type="validate_files",
            success=False,
            status=Status.FAILED,
            message=str(exc),
            data={"error_code": "PREFLIGHT_FAILED"},
        )
        record.error = str(exc)
        record.finished_at = time.time()
        run = _make_run(run_id, f"apply-{source_type}-{source_id}", not execute, [result], root, started_dt)
        _save_record(record, root)
        return run, record

    if not execute:
        results = [
            StepResult(
                step_id=f"file-{index + 1}",
                step_type="write_file",
                success=True,
                status=Status.DRY_RUN,
                message=f"Se aplicaría {entry.path}.",
                data={"path": entry.path, "checksum": entry.checksum},
            )
            for index, (entry, _target) in enumerate(validated)
        ]
        if not results:
            results = [StepResult("no-files", "note", True, Status.OK, "No hay archivos que aplicar.")]
        record.finished_at = time.time()
        run = _make_run(run_id, f"apply-{source_type}-{source_id}", True, results, root, started_dt)
        _save_record(record, root)
        return run, record

    if not approve:
        result = StepResult(
            "approval", "approval", False, Status.NEEDS_APPROVAL,
            "La aplicación real requiere aprobación explícita.",
        )
        record.error = result.message
        record.finished_at = time.time()
        run = _make_run(run_id, f"apply-{source_type}-{source_id}", False, [result], root, started_dt)
        _save_record(record, root)
        return run, record

    journal = _capture_journal(transaction_id, source_type, source_id, validated, root, home_path)
    record.journal_path = _save_journal(journal, root)
    record.backup_snapshot = _backup_snapshot_from_journal(
        journal, f"Antes de aplicar «{label}»", root
    )

    store = ObjectStore(root=root)
    results: list[StepResult] = []
    try:
        for index, ((entry, target), journal_entry) in enumerate(zip(validated, journal.entries), start=1):
            if _fault_after is not None and index > _fault_after:
                raise OSError("Fallo de prueba después de una aplicación parcial")
            store.restore_file(entry.checksum, target, mode=_parse_mode(entry.mode))
            actual, _ = hash_file(str(target))
            if actual != entry.checksum:
                raise ObjectStoreError(f"La verificación posterior falló para {entry.path}")
            from styler.launcher_integrity import normalize_and_inspect
            launcher = normalize_and_inspect(target, home_path, _parse_mode(entry.mode))
            journal_entry.applied = True
            _save_journal(journal, root)
            results.append(
                StepResult(
                    f"file-{index}", "write_file", True, Status.OK,
                    f"Se aplicó {entry.path}.", data={
                        "path": entry.path,
                        "launcher_changed": launcher.changed,
                        "launcher_complete": launcher.complete,
                        "missing_commands": launcher.missing_commands,
                        "missing_paths": launcher.missing_paths,
                        "launcher_notes": launcher.notes,
                    },
                )
            )
        record.applied = True
        record.rollback_status = RollbackStatus.NOT_NEEDED
    except (OSError, ObjectStoreError, ValidationError, ValueError) as exc:
        results.append(
            StepResult(
                f"file-{len(results) + 1}", "write_file", False, Status.FAILED,
                f"La aplicación se detuvo: {exc}", data={"error_code": "APPLY_FAILED"},
            )
        )
        record.error = str(exc)
        rollback_status, rollback_errors = _rollback(journal, root)
        record.rollback_status = rollback_status
        record.rolled_back = rollback_status == RollbackStatus.COMPLETED
        record.rollback_run_id = f"rollback-{transaction_id}"
        if rollback_errors:
            record.error += " | Rollback: " + "; ".join(rollback_errors)
        _save_journal(journal, root)

    record.finished_at = time.time()
    run = _make_run(run_id, f"apply-{source_type}-{source_id}", False, results, root, started_dt)
    # La ejecución no es exitosa si se tuvo que revertir.
    if not record.applied:
        run.success = False
        Path(run.report_path).write_text(json.dumps(run.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    _save_record(record, root)
    return run, record


def apply_snapshot_transactional(
    snapshot_id: str,
    root: str = ".",
    execute: bool = False,
    approve: bool = False,
    observers: list | None = None,
    home: str | Path | None = None,
    _fault_after: int | None = None,
) -> tuple[WorkflowRun, TransactionRecord]:
    # observers se conserva por compatibilidad; el respaldo ahora se limita a
    # las rutas afectadas y no escanea todo el sistema.
    del observers
    snapshot = load_snapshot(snapshot_id, root=root)
    warnings: list[str] = []
    # Este módulo es el plano de ARCHIVOS. Las aplicaciones las instala
    # styler.restore antes de llamar aquí; lo que no se puede prometer es
    # revertirlas, y eso se dice explícitamente.
    if snapshot.state.applications:
        from styler.applications import UNDO_DOES_NOT_UNINSTALL

        warnings.append(UNDO_DOES_NOT_UNINSTALL)
    if snapshot.state.services:
        warnings.append("Los servicios se conservaron como referencia; no se modificaron.")
    run, record = apply_entries_transactional(
        snapshot.state.files,
        source_type="snapshot",
        source_id=snapshot.snapshot_id,
        root=root,
        execute=execute,
        approve=approve,
        label=snapshot.label,
        home=home,
        _fault_after=_fault_after,
    )
    record.warnings = warnings
    _save_record(record, root)
    return run, record


def apply_profile_transactional(
    profile_id: str,
    root: str = ".",
    execute: bool = False,
    approve: bool = False,
    home: str | Path | None = None,
    _fault_after: int | None = None,
) -> tuple[WorkflowRun, TransactionRecord]:
    from styler.profiles import compose_profile, load_profile, load_profile_layers, unresolved_conflicts

    profile = load_profile(profile_id, root=root)
    layers = load_profile_layers(profile, root=root)
    conflicts = unresolved_conflicts(profile, layers)
    if conflicts:
        raise ValueError(
            "El perfil tiene conflictos sin resolver: "
            + ", ".join(conflict.path for conflict in conflicts)
        )
    entries = compose_profile(profile, layers)
    run, record = apply_entries_transactional(
        entries,
        source_type="profile",
        source_id=profile.profile_id,
        root=root,
        execute=execute,
        approve=approve,
        label=profile.name,
        home=home,
        _fault_after=_fault_after,
    )
    if any(layer.applications for layer in layers):
        from styler.applications import UNDO_DOES_NOT_UNINSTALL

        record.warnings.append(UNDO_DOES_NOT_UNINSTALL)
        _save_record(record, root)
    return run, record


def rollback_transaction(transaction_id: str, root: str = ".") -> TransactionRecord:
    record = load_transaction(transaction_id, root=root)
    if not record.journal_path:
        raise ValueError("Esta transacción antigua no tiene journal y no puede revertirse exactamente.")
    journal = load_journal(transaction_id, root=root)
    status, errors = _rollback(journal, root)
    record.rollback_status = status
    record.rolled_back = status == RollbackStatus.COMPLETED
    record.rollback_run_id = f"manual-rollback-{transaction_id}"
    if errors:
        record.error = "; ".join(errors)
    record.finished_at = time.time()
    _save_journal(journal, root)
    _save_record(record, root)
    return record


# --------------------------------------------------------------------------- #
# Olvidar: quitar entradas del registro de cambios
# --------------------------------------------------------------------------- #

class TransactionInUseError(Exception):
    """Se intentó olvidar una transacción que todavía se puede deshacer."""


def is_undoable(record: TransactionRecord) -> bool:
    """¿Esta entrada del registro todavía puede revertirse?"""
    return bool(record.applied and record.journal_path and not record.rolled_back)


def forget_transaction(transaction_id: str, root: str = ".", force: bool = False) -> None:
    """Elimina una entrada del registro de cambios y su diario.

    Regla de seguridad: una transacción que TODAVÍA se puede deshacer no se
    borra por accidente. Hay que pedirlo explícitamente (`force=True`), porque
    al borrar el diario se pierde para siempre la posibilidad de volver atrás.
    Los objetos respaldados siguen en el almacén por contenido; lo que se pierde
    es el mapa para restaurarlos.
    """
    record = load_transaction(transaction_id, root)
    if is_undoable(record) and not force:
        raise TransactionInUseError(
            "Esta aplicación todavía se puede deshacer. Deshazla primero, "
            "o confirma que quieres olvidarla sin poder revertirla."
        )

    record_path = safe_record_path(Path(root) / TRANSACTIONS_DIR, transaction_id)
    journal_path = safe_record_path(Path(root) / JOURNALS_DIR, transaction_id)
    for path in (record_path, journal_path):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - permisos del sistema
            raise TransactionInUseError(
                f"No se pudo borrar {path.name}: {exc}"
            ) from exc


def purge_transactions(root: str = ".", include_undoable: bool = False) -> list[str]:
    """Vacía el registro de cambios. Devuelve los identificadores olvidados.

    Por omisión conserva lo que todavía se puede deshacer: vaciar el historial
    nunca debe quitarle a alguien su única forma de volver atrás.
    """
    forgotten: list[str] = []
    for transaction_id in list_transactions(root):
        try:
            record = load_transaction(transaction_id, root)
        except (OSError, ValueError, ValidationError):
            continue
        if is_undoable(record) and not include_undoable:
            continue
        forget_transaction(transaction_id, root=root, force=True)
        forgotten.append(transaction_id)
    return forgotten
