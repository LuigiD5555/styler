"""Recibos de ejecución y compilación del DAG de reversión.

El plan explica lo que Styler quería hacer; el recibo explica el efecto real. La
reversión se compila desde esos efectos, en orden inverso de finalización.

Desde 0.3.1 un recibo de escritura distingue archivos creados, directorios
creados y archivos sobrescritos con respaldo individual. Nunca se interpreta
un directorio preexistente como algo que pueda borrarse recursivamente.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from styler.runtime.models import ExecutionContext, StepDefinition, WorkflowDefinition

RECEIPTS_ROOT = ".styler/receipts"
CHECKPOINTS_ROOT = ".styler/checkpoints"
RECEIPT_SCHEMA = "styler.receipts/1"


class ReceiptWriteError(OSError):
    """El efecto no pudo quedar registrado con garantías de reversión."""


class ReceiptKind:
    BACKUP_CREATED = "backup_created"
    CHECKPOINT_CREATED = "checkpoint_created"
    PATHS_WRITTEN = "paths_written"
    DIRECTORY_CREATED = "directory_created"
    MARKER_WRITTEN = "marker_written"
    PACKAGE_INSTALLED = "package_installed"
    SETTING_CHANGED = "setting_changed"
    ROLLED_BACK = "rolled_back"

    ALL = {
        BACKUP_CREATED,
        CHECKPOINT_CREATED,
        PATHS_WRITTEN,
        DIRECTORY_CREATED,
        MARKER_WRITTEN,
        PACKAGE_INSTALLED,
        SETTING_CHANGED,
        ROLLED_BACK,
    }


@dataclass(frozen=True)
class StepReceipt:
    receipt_id: str
    change_id: str
    run_id: str
    step_id: str
    step_type: str
    kind: str
    created_at: float
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RECEIPT_SCHEMA,
            "receipt_id": self.receipt_id,
            "change_id": self.change_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "step_type": self.step_type,
            "kind": self.kind,
            "created_at": self.created_at,
            "data": dict(self.data),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StepReceipt":
        return cls(
            receipt_id=str(data.get("receipt_id") or uuid.uuid4().hex),
            change_id=str(data.get("change_id", "")),
            run_id=str(data.get("run_id", "")),
            step_id=str(data.get("step_id", "")),
            step_type=str(data.get("step_type", "")),
            kind=str(data.get("kind", "")),
            created_at=float(data.get("created_at", 0.0)),
            data=dict(data.get("data") or {}),
        )


class ReceiptJournal:
    """Registro append-only por cambio, en JSONL."""

    def __init__(self, root: str | Path, change_id: str) -> None:
        if not change_id.strip():
            raise ValueError("change_id no puede estar vacío.")
        self.change_id = change_id.strip()
        self.path = Path(root) / RECEIPTS_ROOT / f"{self.change_id}.jsonl"

    def ensure_writable(self) -> None:
        """Comprueba el diario antes de producir un efecto."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8"):
            pass

    def append(self, receipt: StepReceipt) -> StepReceipt:
        self.ensure_writable()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt.to_dict(), ensure_ascii=False) + "\n")
            handle.flush()
        return receipt

    def record(
        self,
        *,
        run_id: str,
        step_id: str,
        step_type: str,
        kind: str,
        data: Mapping[str, Any] | None = None,
        clock: Any = time.time,
    ) -> StepReceipt:
        if kind not in ReceiptKind.ALL:
            raise ValueError(f"Tipo de recibo desconocido: {kind}")
        return self.append(
            StepReceipt(
                receipt_id=uuid.uuid4().hex,
                change_id=self.change_id,
                run_id=run_id,
                step_id=step_id,
                step_type=step_type,
                kind=kind,
                created_at=float(clock()),
                data=dict(data or {}),
            )
        )

    def entries(self) -> tuple[StepReceipt, ...]:
        if not self.path.exists():
            return ()
        items: list[StepReceipt] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(StepReceipt.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return tuple(items)

    def pending_undo(self) -> tuple[StepReceipt, ...]:
        undone = {
            str(entry.data.get("receipt_id"))
            for entry in self.entries()
            if entry.kind == ReceiptKind.ROLLED_BACK
        }
        return tuple(
            entry
            for entry in self.entries()
            if entry.kind != ReceiptKind.ROLLED_BACK and entry.receipt_id not in undone
        )

    def latest_pending_for_step(
        self,
        step_id: str,
        *,
        kind: str | None = None,
    ) -> StepReceipt | None:
        """Último efecto vivo de un paso, útil para continuar tras un fallo."""
        for receipt in reversed(self.pending_undo()):
            if receipt.step_id != step_id:
                continue
            if kind is not None and receipt.kind != kind:
                continue
            return receipt
        return None

    def mark_rolled_back(
        self, receipts: Iterable[StepReceipt], *, run_id: str, clock: Any = time.time
    ) -> None:
        for receipt in receipts:
            self.record(
                run_id=run_id,
                step_id=receipt.step_id,
                step_type="undo",
                kind=ReceiptKind.ROLLED_BACK,
                data={"receipt_id": receipt.receipt_id, "undone_kind": receipt.kind},
                clock=clock,
            )


def journal_for(ctx: ExecutionContext) -> ReceiptJournal | None:
    change_id = str(ctx.values.get("change_id") or "")
    if not change_id:
        return None
    return ReceiptJournal(ctx.values.get("receipts_root") or ctx.root, change_id)


def ensure_receipts_writable(ctx: ExecutionContext) -> ReceiptJournal | None:
    """Falla antes del efecto si un cambio reversible no puede llevar diario."""
    if ctx.dry_run:
        return None
    journal = journal_for(ctx)
    if journal is None:
        return None
    try:
        journal.ensure_writable()
    except OSError as exc:
        raise ReceiptWriteError(
            f"No se puede escribir el diario de reversión {journal.path}: {exc}"
        ) from exc
    return journal


def emit_receipt(
    ctx: ExecutionContext,
    step: StepDefinition,
    kind: str,
    data: Mapping[str, Any] | None = None,
) -> StepReceipt | None:
    """Registra un efecto reversible; un fallo nunca se oculta."""
    if ctx.dry_run:
        return None
    journal = journal_for(ctx)
    if journal is None:
        return None
    try:
        return journal.record(
            run_id=ctx.run_id,
            step_id=step.id,
            step_type=step.step_type,
            kind=kind,
            data=data or {},
        )
    except OSError as exc:
        message = f"El efecto ocurrió, pero no pudo registrarse en {journal.path}: {exc}"
        ctx.values.setdefault("receipt_errors", []).append(message)
        raise ReceiptWriteError(message) from exc


UNDO_STEP_TYPES = {
    ReceiptKind.BACKUP_CREATED: "undo_restore_backup",
    ReceiptKind.CHECKPOINT_CREATED: "undo_restore_checkpoint",
    ReceiptKind.PATHS_WRITTEN: "undo_remove_paths",
    ReceiptKind.DIRECTORY_CREATED: "undo_remove_paths",
    ReceiptKind.MARKER_WRITTEN: "undo_remove_paths",
    ReceiptKind.PACKAGE_INSTALLED: "uninstall_package",
    ReceiptKind.SETTING_CHANGED: "undo_restore_setting",
}


def _receipt_paths(receipt: StepReceipt) -> tuple[Path, ...]:
    """Rutas que un efecto o su reversión puede tocar.

    Sirven para construir dependencias del DAG de reversión. Dos recibos de
    ramas independientes pueden deshacerse en paralelo; dos recibos que tocan
    la misma ruta o rutas anidadas conservan el orden contrario al efecto.
    """
    values: list[str] = []
    data = receipt.data
    source = data.get("source")
    if source:
        values.append(str(source))
    for key in ("created_paths", "created_directories"):
        raw = data.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        values.extend(str(item) for item in raw if item)
    overwritten = data.get("overwritten") or []
    if isinstance(overwritten, list):
        values.extend(
            str(item.get("path"))
            for item in overwritten
            if isinstance(item, Mapping) and item.get("path")
        )
    checkpoint_paths = data.get("paths") or []
    if isinstance(checkpoint_paths, list):
        values.extend(
            str(item.get("path"))
            for item in checkpoint_paths
            if isinstance(item, Mapping) and item.get("path")
        )
    seen: set[str] = set()
    paths: list[Path] = []
    for raw in values:
        if raw in seen:
            continue
        seen.add(raw)
        paths.append(Path(raw))
    return tuple(paths)


def _paths_conflict(left: Path, right: Path) -> bool:
    try:
        left = left.resolve(strict=False)
        right = right.resolve(strict=False)
    except OSError:
        pass
    return left == right or left in right.parents or right in left.parents


def _receipts_conflict(left: StepReceipt, right: StepReceipt) -> bool:
    left_paths = _receipt_paths(left)
    right_paths = _receipt_paths(right)
    return any(_paths_conflict(a, b) for a in left_paths for b in right_paths)


def _undo_resource(receipt: StepReceipt) -> list[str]:
    paths = _receipt_paths(receipt)
    if not paths:
        return []
    # El scheduler entiende igualdad exacta. Usamos la raíz afectada más corta;
    # las dependencias por anidamiento aportan la protección adicional.
    root = min(paths, key=lambda item: len(item.parts))
    return [f"filesystem:{root}"]


def compile_rollback_workflow(
    receipts: Sequence[StepReceipt],
    *,
    name: str = "undo",
    description: str = "",
    package_protections: Mapping[str, Sequence[str]] | None = None,
) -> WorkflowDefinition:
    """Compila un DAG de reversión propio desde efectos confirmados.

    No invierte el Apply DAG. Cada recibo produce una operación de reversión y
    las aristas se calculan por conflicto de rutas. Efectos independientes son
    ramas paralelas; cuando dos efectos se superponen, se deshace primero el
    más reciente. Las decisiones humanas sobre paquetes quedan al final.
    """
    ordered = sorted(receipts, key=lambda item: item.created_at, reverse=True)
    steps: list[StepDefinition] = []
    receipt_by_step: dict[str, StepReceipt] = {}

    for index, receipt in enumerate(ordered):
        step_type = UNDO_STEP_TYPES.get(receipt.kind)
        if step_type is None:
            continue
        step_id = f"undo.{index:02d}.{receipt.step_id}"
        config: dict[str, Any] = {
            "receipt_id": receipt.receipt_id,
            "receipt_kind": receipt.kind,
            **dict(receipt.data),
        }
        if receipt.kind == ReceiptKind.PACKAGE_INSTALLED:
            manager = str(receipt.data.get("manager") or "")
            package = str(receipt.data.get("package") or "")
            protection_key = f"{manager}:{package}"
            config["protected_by_changes"] = list(
                (package_protections or {}).get(protection_key, ())
            )
        step = StepDefinition(
            id=step_id,
            step_type=step_type,
            description=_undo_description(receipt),
            needs=[],
            config=config,
            required=True,
            risk="high" if receipt.kind == ReceiptKind.PACKAGE_INSTALLED else "low",
            exclusive_resources=(
                [f"package-manager:{receipt.data.get('manager', '')}"]
                if receipt.kind == ReceiptKind.PACKAGE_INSTALLED
                else _undo_resource(receipt)
            ),
            phase="undo",
        )
        steps.append(step)
        receipt_by_step[step_id] = receipt

    if not steps:
        steps.append(
            StepDefinition(
                id="undo.nothing",
                step_type="note",
                description="No hay efectos registrados que revertir.",
                config={"message": "No hay efectos registrados que revertir."},
                phase="undo",
            )
        )
    else:
        by_id = {step.id: step for step in steps}
        filesystem_ids = [
            step.id
            for step in steps
            if receipt_by_step[step.id].kind != ReceiptKind.PACKAGE_INSTALLED
        ]

        # ordered está de más reciente a más antiguo. Si dos efectos se pisan,
        # el paso antiguo espera a que se revierta el efecto más reciente.
        for newer_index, newer_step in enumerate(steps):
            newer_receipt = receipt_by_step[newer_step.id]
            if newer_receipt.kind == ReceiptKind.PACKAGE_INSTALLED:
                continue
            for older_step in steps[newer_index + 1 :]:
                older_receipt = receipt_by_step[older_step.id]
                if older_receipt.kind == ReceiptKind.PACKAGE_INSTALLED:
                    continue
                if _receipts_conflict(newer_receipt, older_receipt):
                    older_step.needs.append(newer_step.id)

        # La desinstalación de paquetes pertenece al Undo DAG y ocurre al
        # final, después de restablecer archivos y configuración.
        for step in steps:
            receipt = receipt_by_step[step.id]
            if receipt.kind == ReceiptKind.PACKAGE_INSTALLED:
                step.needs = list(filesystem_ids)

        # Deduplicación estable de aristas.
        for step in by_id.values():
            step.needs = list(dict.fromkeys(step.needs))

    edge_count = sum(len(step.needs) for step in steps)
    roots = [step.id for step in steps if not step.needs]
    run_ids = sorted({receipt.run_id for receipt in receipts if receipt.run_id})
    return WorkflowDefinition(
        name=name,
        steps=steps,
        description=description or "Reversión compilada desde los recibos de ejecución.",
        operation="undo",
        metadata={
            "compiled_from": "receipts",
            "receipt_count": len(receipts),
            "edge_count": edge_count,
            "parallel_roots": roots,
            "inverse_of_runs": run_ids,
            "strategy": "effect-conflict-dag",
        },
    )


def _undo_description(receipt: StepReceipt) -> str:
    if receipt.kind == ReceiptKind.BACKUP_CREATED:
        return f"Restaurar {receipt.data.get('source', '')} desde el respaldo."
    if receipt.kind == ReceiptKind.PACKAGE_INSTALLED:
        return (
            f"Desinstalar {receipt.data.get('package', 'el paquete')} mediante "
            f"{receipt.data.get('manager', 'su gestor')}, porque Styler lo instaló "
            "durante esta aplicación."
        )
    if receipt.kind == ReceiptKind.SETTING_CHANGED:
        return (
            f"Restaurar el ajuste {receipt.data.get('backend', '')}:"
            f"{receipt.data.get('schema', '')}:{receipt.data.get('key', '')}."
        )
    created = receipt.data.get("created_paths") or []
    overwritten = receipt.data.get("overwritten") or []
    return (
        f"Revertir {len(created)} elemento(s) creados y "
        f"{len(overwritten)} sobrescrito(s) por {receipt.step_id}."
    )


def all_checkpoint_receipts(root: str | Path) -> list[StepReceipt]:
    """Todos los recibos ``CHECKPOINT_CREATED`` vivos bajo ``root``."""
    root = Path(root)
    receipts: list[StepReceipt] = []
    receipts_root = root / RECEIPTS_ROOT
    if receipts_root.is_dir():
        for path in sorted(receipts_root.glob("*.jsonl")):
            change_id = path.stem
            try:
                receipts.extend(
                    item
                    for item in ReceiptJournal(root, change_id).pending_undo()
                    if item.kind == ReceiptKind.CHECKPOINT_CREATED
                )
            except (OSError, ValueError):
                continue
    return receipts


def prune_system_checkpoints(root: str | Path, *, keep: int = 5) -> tuple[str, ...]:
    """Poda checkpoints vivos más allá de los ``keep`` más recientes.

    Un checkpoint nunca se poda si su cambio todavía tiene recibos vivos
    distintos al propio checkpoint: la promesa de Deshacer no puede depender
    de un respaldo que ya fue borrado por retención.
    """
    root = Path(root)
    ordered = sorted(all_checkpoint_receipts(root), key=lambda item: item.created_at, reverse=True)
    pruned_ids: list[str] = []
    for receipt in ordered[keep:]:
        change_id = str(receipt.data.get("change_id") or receipt.change_id or "")
        if change_id:
            other_live_receipts = any(
                item.kind != ReceiptKind.CHECKPOINT_CREATED
                for item in ReceiptJournal(root, change_id).pending_undo()
            )
            if other_live_receipts:
                continue
        checkpoint_id = str(receipt.data.get("checkpoint_id") or "")
        backup_dir = root / CHECKPOINTS_ROOT / checkpoint_id
        if checkpoint_id and backup_dir.is_dir():
            import shutil

            shutil.rmtree(backup_dir, ignore_errors=True)
        journal = ReceiptJournal(root, receipt.change_id)
        journal.record(
            run_id=receipt.run_id,
            step_id=receipt.step_id,
            step_type="retention",
            kind=ReceiptKind.ROLLED_BACK,
            data={
                "receipt_id": receipt.receipt_id,
                "undone_kind": receipt.kind,
                "reason": "retention_pruned",
            },
        )
        pruned_ids.append(checkpoint_id)
    return tuple(pruned_ids)
