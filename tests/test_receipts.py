"""Recibos de ejecución y DAG de reversión compilado desde ellos."""
from __future__ import annotations
from styler.execution.registry import default_registry

from pathlib import Path

import pytest

from styler.receipts import (
    ReceiptJournal,
    ReceiptKind,
    StepReceipt,
    compile_rollback_workflow,
    emit_receipt,
)
from styler.execution.base import ExecutorRegistry
from styler.planning.graph import drop_step, topological_order
from styler.planning.models import ExecutionContext, StepDefinition, WorkflowDefinition


def _journal(tmp_path: Path) -> ReceiptJournal:
    return ReceiptJournal(tmp_path, "photogimp")


def test_journal_is_append_only(tmp_path):
    journal = _journal(tmp_path)
    journal.record(run_id="r1", step_id="a", step_type="t", kind=ReceiptKind.PATHS_WRITTEN)
    journal.record(run_id="r1", step_id="b", step_type="t", kind=ReceiptKind.MARKER_WRITTEN)
    assert len(journal.entries()) == 2
    assert journal.path.read_text().count("\n") == 2


def test_rolled_back_receipts_leave_pending_undo_empty(tmp_path):
    journal = _journal(tmp_path)
    first = journal.record(run_id="r1", step_id="a", step_type="t", kind=ReceiptKind.PATHS_WRITTEN)
    assert journal.pending_undo() == (first,)
    journal.mark_rolled_back([first], run_id="r2")
    assert journal.pending_undo() == ()
    # El historial conserva ambas cosas: lo aplicado y lo deshecho.
    assert len(journal.entries()) == 2


def test_corrupt_line_does_not_invalidate_history(tmp_path):
    journal = _journal(tmp_path)
    journal.record(run_id="r1", step_id="a", step_type="t", kind=ReceiptKind.PATHS_WRITTEN)
    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write("{esto no es json\n")
    assert len(journal.entries()) == 1


def test_unknown_receipt_kind_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        _journal(tmp_path).record(run_id="r", step_id="a", step_type="t", kind="inventado")


def test_emit_receipt_is_silent_in_dry_run(tmp_path):
    ctx = ExecutionContext(root=tmp_path, dry_run=True, values={"change_id": "photogimp"})
    step = StepDefinition(id="s", step_type="install_overlay")
    assert emit_receipt(ctx, step, ReceiptKind.PATHS_WRITTEN, {"created_paths": []}) is None
    assert not (tmp_path / ".styler" / "receipts").exists()


def test_emit_receipt_requires_a_change_id(tmp_path):
    ctx = ExecutionContext(root=tmp_path, dry_run=False, values={})
    step = StepDefinition(id="s", step_type="install_overlay")
    assert emit_receipt(ctx, step, ReceiptKind.PATHS_WRITTEN, {"created_paths": []}) is None


def _receipt(step_id: str, kind: str, created_at: float, **data) -> StepReceipt:
    return StepReceipt(
        receipt_id=f"id-{step_id}",
        change_id="photogimp",
        run_id="run",
        step_id=step_id,
        step_type="x",
        kind=kind,
        created_at=created_at,
        data=data,
    )


def test_rollback_is_a_distinct_effect_dag_in_reverse_completion_order():
    receipts = [
        _receipt("backup", ReceiptKind.BACKUP_CREATED, 10.0, source="/s", backup="/b"),
        _receipt("install", ReceiptKind.PATHS_WRITTEN, 20.0, created_paths=["/p"]),
        _receipt("marker", ReceiptKind.MARKER_WRITTEN, 30.0, created_paths=["/m"]),
    ]
    workflow = compile_rollback_workflow(receipts)
    assert [step.step_type for step in workflow.steps] == [
        "undo_remove_paths",
        "undo_remove_paths",
        "undo_restore_backup",
    ]
    # Son efectos independientes: el Undo DAG puede tratarlos como ramas.
    assert workflow.operation == "undo"
    assert all(step.needs == [] for step in workflow.steps)
    assert workflow.metadata["strategy"] == "effect-conflict-dag"
    assert topological_order(workflow.steps) == [step.id for step in workflow.steps]


def test_rollback_without_receipts_says_so_instead_of_failing():
    workflow = compile_rollback_workflow([])
    assert len(workflow.steps) == 1
    assert workflow.steps[0].step_type == "note"


def test_package_installed_by_apply_becomes_an_uninstall_node():
    workflow = compile_rollback_workflow(
        [_receipt("gimp", ReceiptKind.PACKAGE_INSTALLED, 1.0, package="gimp", manager="flatpak")]
    )
    assert workflow.steps[0].step_type == "uninstall_package"


def test_undo_step_types_are_registered():
    known = default_registry().known_types()
    assert {"undo_restore_backup", "undo_remove_paths", "uninstall_package", "undo_note"} <= known


# --------------------------------------------------------------- drop_step


def test_drop_step_rewires_dependents():
    workflow = WorkflowDefinition(
        name="w",
        steps=[
            StepDefinition(id="install", step_type="note"),
            StepDefinition(id="backup", step_type="backup_config", needs=["install"]),
            StepDefinition(id="overlay", step_type="install_overlay", needs=["backup"]),
        ],
    )
    reduced = drop_step(workflow, "backup")
    assert [step.id for step in reduced.steps] == ["install", "overlay"]
    assert reduced.steps[1].needs == ["install"]
    # El grafo resultante sigue siendo válido: nadie apunta a un paso inexistente.
    assert topological_order(reduced.steps) == ["install", "overlay"]


def test_drop_step_ignores_unknown_ids():
    workflow = WorkflowDefinition(name="w", steps=[StepDefinition(id="a", step_type="note")])
    assert drop_step(workflow, "no-existe") is workflow
