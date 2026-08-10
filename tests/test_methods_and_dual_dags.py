from __future__ import annotations

import json
import time
from pathlib import Path

from styler.changes import ChangeService
from styler.methods import (
    Mechanism,
    MethodContext,
    MethodPolicy,
    Operation,
    annotate_workflow_methods,
    default_method_registry,
)
from styler.receipts import ReceiptKind, StepReceipt, compile_rollback_workflow
from styler.runtime.engine import WorkflowEngine
from styler.runtime.models import ExecutionContext, StepDefinition, WorkflowDefinition
from styler.runtime.executors import ExecutorRegistry, StepExecutor
from styler.runtime.models import Status, StepResult


def _receipt(step_id: str, kind: str, created_at: float, **data) -> StepReceipt:
    return StepReceipt(
        receipt_id=f"receipt-{step_id}",
        change_id="photogimp",
        run_id="apply-run",
        step_id=step_id,
        step_type="effect",
        kind=kind,
        created_at=created_at,
        data=data,
    )


def test_terminal_first_selects_native_scoped_overlay():
    registry = default_method_registry()
    selection = registry.select(
        Operation.OVERLAY_APPLY,
        context=MethodContext(frozenset({"rsync"})),
        policy=MethodPolicy(prefer_terminal=True, allow_gui_input=False),
    )
    assert selection.chosen.method.method_id == "overlay.python-scoped"
    assert selection.chosen.method.mechanism == Mechanism.NATIVE_API
    alternatives = {item.method.method_id: item for item in selection.candidates}
    assert alternatives["overlay.rsync-scoped"].available is False


def test_gui_input_is_not_an_eligible_application_method():
    selection = default_method_registry().select(
        Operation.APPLICATION_INITIALIZE,
        context=MethodContext(frozenset({"gdbus"})),
        policy=MethodPolicy(prefer_terminal=True, allow_gui_input=False),
    )
    assert selection.chosen.method.method_id == "app.registered-cli"
    gui = next(item for item in selection.candidates if item.method.method_id == "app.gui-input")
    assert gui.available is False


def test_annotation_records_ordered_internal_semantic_methods():
    workflow = WorkflowDefinition(
        name="initialize",
        operation="apply",
        steps=[
            StepDefinition(
                id="gimp.initialize",
                step_type="initialize_flatpak_app",
                config={
                    "semantic_operations": [
                        {"operation": "application.launch", "label": "abrir"},
                        {"operation": "wait.observable", "label": "esperar"},
                        {"operation": "application.stop", "label": "cerrar"},
                    ]
                },
            )
        ],
    )
    annotated = annotate_workflow_methods(workflow, context=MethodContext())
    step = annotated.steps[0]
    assert step.method_id == "app.registered-cli"
    sequence = step.config["selected_semantic_sequence"]
    assert [item["operation"] for item in sequence] == [
        "application.launch",
        "wait.observable",
        "application.stop",
    ]
    assert [item["method_id"] for item in sequence] == [
        "app.registered-launch",
        "wait.observable-condition",
        "app.process-terminate",
    ]


def test_apply_and_undo_are_two_distinct_workflows(tmp_path: Path):
    service = ChangeService(tmp_path / "library", tmp_path / "home")
    journal = service.journal_for_change("photogimp")
    journal.record(
        run_id="apply-1",
        step_id="overlay",
        step_type="install_overlay",
        kind=ReceiptKind.PATHS_WRITTEN,
        data={"created_paths": [str(tmp_path / "home/.config/GIMP/new-file")]},
    )
    pair = service.workflow_pair("photogimp", "flatpak")
    assert pair.apply is not pair.undo
    assert pair.apply.operation == "apply"
    assert pair.undo.operation == "undo"
    assert pair.undo.metadata["compiled_from"] == "receipts"
    assert pair.undo_available is True


def test_independent_undo_effects_are_parallel_roots():
    workflow = compile_rollback_workflow(
        [
            _receipt("one", ReceiptKind.PATHS_WRITTEN, 2, created_paths=["/home/u/a/file"]),
            _receipt("two", ReceiptKind.PATHS_WRITTEN, 1, created_paths=["/home/u/b/file"]),
        ]
    )
    assert len(workflow.metadata["parallel_roots"]) == 2
    assert all(not step.needs for step in workflow.steps)


def test_nested_undo_effects_preserve_reverse_effect_order():
    workflow = compile_rollback_workflow(
        [
            _receipt("backup", ReceiptKind.BACKUP_CREATED, 1, source="/home/u/.config/GIMP", backup="/b"),
            _receipt("overlay", ReceiptKind.PATHS_WRITTEN, 2, created_paths=["/home/u/.config/GIMP/3.0/x"]),
        ]
    )
    overlay, backup = workflow.steps
    assert backup.needs == [overlay.id]


def test_package_uninstall_waits_for_filesystem_undo():
    workflow = compile_rollback_workflow(
        [
            _receipt("package", ReceiptKind.PACKAGE_INSTALLED, 3, package="gimp"),
            _receipt("overlay", ReceiptKind.PATHS_WRITTEN, 2, created_paths=["/home/u/x"]),
        ]
    )
    package = next(step for step in workflow.steps if step.step_type == "uninstall_package")
    filesystem = next(step for step in workflow.steps if step.step_type == "undo_remove_paths")
    assert package.needs == [filesystem.id]


def test_engine_writes_semantic_trace_with_selected_method(tmp_path: Path):
    workflow = annotate_workflow_methods(
        WorkflowDefinition(
            name="trace",
            operation="apply",
            steps=[StepDefinition("note", "note", config={"message": "ok"})],
        ),
        context=MethodContext(),
    )
    run = WorkflowEngine().run(workflow, ExecutionContext(root=tmp_path, dry_run=True))
    trace = json.loads(Path(run.trace_path).read_text(encoding="utf-8"))
    assert trace["operation"] == "apply"
    assert trace["planned_order"] == ["note"]
    assert trace["actual_start_order"] == ["note"]
    assert trace["actual_finish_order"] == ["note"]
    assert trace["nodes"][0]["semantic_operation"] == "note"
    assert trace["nodes"][0]["method_id"] == "note.no-effect"
    assert trace["nodes"][0]["execution_start_index"] == 1
    assert trace["nodes"][0]["execution_finish_index"] == 1



class _DelayExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "delay_for_trace"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        time.sleep(float(step.config.get("seconds", 0)))
        return StepResult(step.id, step.step_type, True, Status.OK, "done")


def test_semantic_trace_keeps_planned_and_actual_parallel_order(tmp_path: Path):
    registry = ExecutorRegistry.default()
    registry.register(_DelayExecutor())
    workflow = WorkflowDefinition(
        name="parallel-trace",
        operation="apply",
        metadata={"max_workers": 2},
        steps=[
            StepDefinition("slow", "delay_for_trace", config={"seconds": 0.05}),
            StepDefinition("fast", "delay_for_trace", config={"seconds": 0.005}),
        ],
    )
    run = WorkflowEngine(registry).run(workflow, ExecutionContext(root=tmp_path, dry_run=False))
    trace = json.loads(Path(run.trace_path).read_text(encoding="utf-8"))
    assert trace["planned_order"] == ["slow", "fast"]
    assert set(trace["actual_start_order"]) == {"slow", "fast"}
    assert trace["actual_finish_order"] == ["fast", "slow"]
    by_id = {item["node_id"]: item for item in trace["nodes"]}
    assert {by_id["slow"]["execution_start_index"], by_id["fast"]["execution_start_index"]} == {1, 2}
    assert by_id["fast"]["execution_finish_index"] == 1

def test_engine_rejects_method_not_implemented_by_executor(tmp_path: Path):
    workflow = WorkflowDefinition(
        name="bad-method",
        operation="apply",
        steps=[
            StepDefinition(
                "note",
                "note",
                method_id="overlay.python-scoped",
                operation="note",
            )
        ],
    )
    errors = WorkflowEngine().validate(workflow)
    assert any("solo implementa" in error for error in errors)


def test_terminal_change_plan_exposes_apply_dag_and_methods(tmp_path: Path, capsys):
    from styler.cli import main

    code = main([
        "change", "plan", "photogimp",
        "--operation", "apply",
        "--methods",
        "--root", str(tmp_path / "library"),
        "--home", str(tmp_path / "home"),
    ])
    output = capsys.readouterr().out
    assert code == 0
    assert "APPLY DAG" in output
    assert "wait.observable-condition" in output
    assert "overlay.python-scoped" in output


def test_terminal_trace_reads_recorded_semantic_order(tmp_path: Path, capsys):
    from styler.cli import main

    workflow = annotate_workflow_methods(
        WorkflowDefinition(
            name="trace-cli",
            operation="apply",
            steps=[StepDefinition("note", "note", config={"message": "ok"})],
        ),
        context=MethodContext(),
    )
    run = WorkflowEngine().run(workflow, ExecutionContext(root=tmp_path, dry_run=True))
    code = main(["change", "trace", run.trace_path, "--root", str(tmp_path)])
    output = capsys.readouterr().out
    assert code == 0
    assert "APPLY" in output
    assert "note.no-effect" in output
