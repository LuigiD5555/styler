from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from styler.models import Changeset, Component, Decision, FileEntry, Package
from styler.planning.builder import workflow_from_changeset
from tests.support.local_engine import WorkflowEngine
from styler.execution.base import ExecutorRegistry, StepExecutor
from styler.planning.models import (
    ExecutionContext,
    Status,
    StepDefinition,
    StepResult,
    WorkflowDefinition,
)
from styler.planning.graph import DependencyCycleError, topological_order


def included_changeset() -> Changeset:
    """Changeset mínimo para probar la compilación semántica a workflow.

    Estas pruebas ya no dependen del antiguo pipeline prototype
    ``State -> diff -> interpreter -> review``. Ese flujo no tenía consumidores
    productivos; el constructor/catálogo actual produce componentes directamente.
    """
    package_component = Component(
        component_id="pkg-apt-gimp",
        title="Instalar GIMP",
        category="aplicaciones",
        packages=[Package(manager="apt", name="gimp", version="2.10.36-3")],
        decision=Decision.INCLUDE,
    )
    custom_component = Component(
        component_id="customize-gimp",
        title="PhotoGIMP",
        category="aplicaciones",
        depends_on=[package_component.component_id],
        files=[
            FileEntry(
                path="${HOME}/.config/GIMP/2.10/menurc",
                checksum="h1",
                size=10,
                owner_hint="user",
            )
        ],
        decision=Decision.INCLUDE,
    )
    theme_component = Component(
        component_id="files-themes-sweet",
        title="Tema Sweet",
        category="apariencia",
        files=[
            FileEntry(
                path="${HOME}/.themes/Sweet/index.theme",
                checksum="h2",
                size=20,
                owner_hint="user",
            )
        ],
        decision=Decision.INCLUDE,
    )
    return Changeset(
        changeset_id="base-target",
        base_state="base",
        target_state="target",
        components=[package_component, custom_component, theme_component],
    )


def test_workflow_respects_component_dependencies_without_yaml():
    workflow = workflow_from_changeset(included_changeset(), name="test")
    steps = {step.id: step for step in workflow.steps}
    overlay = steps["customize-gimp__overlay"]
    assert "pkg-apt-gimp__install" in overlay.needs
    order = WorkflowEngine().plan(workflow)
    assert order.index("pkg-apt-gimp__install") < order.index("customize-gimp__overlay")


def test_ignored_components_do_not_create_steps():
    changeset = included_changeset()
    for component in changeset.components:
        component.decision = Decision.IGNORED
    workflow = workflow_from_changeset(changeset, name="empty")
    assert workflow.steps == []
    errors = WorkflowEngine().validate(workflow)
    assert any("al menos un paso" in error for error in errors)


def test_stable_topological_order_and_cycle_detection():
    steps = [
        StepDefinition("package", "note", needs=["test"]),
        StepDefinition("test", "note", needs=["lint"]),
        StepDefinition("lint", "note"),
    ]
    assert topological_order(steps) == ["lint", "test", "package"]

    cycle = [
        StepDefinition("a", "note", needs=["b"]),
        StepDefinition("b", "note", needs=["a"]),
    ]
    try:
        topological_order(cycle)
    except DependencyCycleError as exc:
        assert exc.remaining == ["a", "b"]
    else:
        raise AssertionError("El ciclo debía detectarse")


def test_approval_gate_blocks_sensitive_step():
    workflow = WorkflowDefinition(
        name="approval",
        steps=[StepDefinition("gate", "note", requires_approval=True)],
    )
    with TemporaryDirectory() as temp:
        run = WorkflowEngine().run(
            workflow,
            ExecutionContext(root=Path(temp), dry_run=True, approve=False),
        )
    assert run.success is False
    assert run.results[0].status == Status.NEEDS_APPROVAL


class FlakyExecutor(StepExecutor):
    def __init__(self):
        self.calls = 0

    @property
    def step_type(self) -> str:
        return "flaky"

    def run(self, step, ctx):
        self.calls += 1
        if self.calls == 1:
            return StepResult.failed(step, "fallo temporal", "TEMPORARY")
        return StepResult(step.id, step.step_type, True, Status.OK, "recuperado")


def test_retries_are_covered_by_the_test_harness():
    """Conserva prueba unitaria de retries sin reintroducir runtime productivo Python."""
    flaky = FlakyExecutor()
    registry = ExecutorRegistry()
    registry.register(flaky)
    workflow = WorkflowDefinition(
        name="retry",
        steps=[StepDefinition("unstable", "flaky", retries=1)],
    )
    with TemporaryDirectory() as temp:
        run = WorkflowEngine(registry).run(
            workflow,
            ExecutionContext(root=Path(temp), dry_run=True),
        )
    assert run.success is True
    assert flaky.calls == 2
    assert run.results[0].data["attempts"] == 2


def test_test_harness_report_is_written_under_styler_directory():
    workflow = WorkflowDefinition(name="report", steps=[StepDefinition("hello", "note")])
    with TemporaryDirectory() as temp:
        run = WorkflowEngine().run(
            workflow,
            ExecutionContext(root=Path(temp), dry_run=True),
        )
        report = Path(run.report_path)
        assert report.exists()
        assert ".styler/runs" in report.as_posix()
        text = report.read_text(encoding="utf-8")
        assert '"workflow": "report"' in text


def test_from_and_only_select_steps_in_resolved_order_in_test_harness():
    workflow = WorkflowDefinition(
        name="selection",
        steps=[
            StepDefinition("a", "note"),
            StepDefinition("b", "note", needs=["a"]),
            StepDefinition("c", "note", needs=["b"]),
        ],
    )
    with TemporaryDirectory() as temp:
        run = WorkflowEngine().run(
            workflow,
            ExecutionContext(
                root=Path(temp),
                dry_run=True,
                from_step="b",
                only_steps=["c"],
            ),
        )
    assert [result.node_id for result in run.results] == ["a", "b", "c"]
    assert [result.status for result in run.results] == ["skipped", "skipped", "blocked"]
    assert run.success is False
