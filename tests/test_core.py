from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from styler.diff import diff_states
from styler.interpreter import interpret
from styler.models import Decision, FileEntry, Package, State
from styler.review import auto_decide
from styler.runtime.builder import workflow_from_changeset
from styler.runtime.engine import WorkflowEngine
from styler.runtime.executors import ExecutorRegistry, StepExecutor
from styler.runtime.models import (
    ExecutionContext,
    Status,
    StepDefinition,
    StepResult,
    WorkflowDefinition,
)
from styler.runtime.graph import DependencyCycleError, topological_order


def make_states():
    base = State(state_id="base", label="base")
    target = State(
        state_id="target",
        label="target",
        packages=[Package(manager="apt", name="gimp", version="2.10.36-3")],
        files=[
            FileEntry(path="${HOME}/.config/GIMP/2.10/menurc", checksum="h1", size=10, owner_hint="user"),
            FileEntry(path="${HOME}/.themes/Sweet/index.theme", checksum="h2", size=20, owner_hint="user"),
        ],
    )
    return base, target


def included_changeset():
    base, target = make_states()
    changeset = interpret(base.state_id, target.state_id, diff_states(base, target))
    auto_decide(changeset, default=Decision.INCLUDE)
    return changeset


def test_diff_detects_added_package_and_files():
    base, target = make_states()
    changes = diff_states(base, target)
    kinds = {change.kind.value for change in changes}
    assert "package_added" in kinds
    assert "file_added" in kinds
    assert len(changes) == 3


def test_interpreter_links_customization_to_its_package():
    changeset = included_changeset()
    package_component = next(
        component for component in changeset.components if component.component_id == "pkg-apt-gimp"
    )
    custom_component = next(
        component for component in changeset.components if component.component_id == "customize-gimp"
    )
    assert package_component.depends_on == []
    assert custom_component.depends_on == [package_component.component_id]
    assert all("GIMP" in entry.path for entry in custom_component.files)


def test_interpreter_groups_unrelated_files_separately():
    changeset = included_changeset()
    theme_component = next(
        component for component in changeset.components if "themes" in component.component_id
    )
    assert theme_component.category == "apariencia"


def test_workflow_respects_component_dependencies_without_yaml():
    workflow = workflow_from_changeset(included_changeset(), name="test")
    steps = {step.id: step for step in workflow.steps}
    overlay = steps["customize-gimp__overlay"]
    assert "pkg-apt-gimp__install" in overlay.needs
    order = WorkflowEngine().plan(workflow)
    assert order.index("pkg-apt-gimp__install") < order.index("customize-gimp__overlay")


def test_ignored_components_do_not_create_steps():
    base, target = make_states()
    changeset = interpret(base.state_id, target.state_id, diff_states(base, target))
    auto_decide(changeset, default=Decision.IGNORED)
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


def test_retries_are_integrated_in_the_internal_engine():
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


def test_run_report_is_written_under_styler_directory():
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


def test_from_and_only_select_steps_in_resolved_order():
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


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"OK   {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} pruebas pasaron")
    raise SystemExit(1 if failures else 0)
