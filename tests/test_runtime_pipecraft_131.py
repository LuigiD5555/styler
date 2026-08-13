from __future__ import annotations

import json
from pathlib import Path

from styler.runtime.engine import WorkflowEngine
from styler.runtime.executors import ExecutorRegistry, StepExecutor
from styler.runtime.models import (
    CheckAttachment,
    CheckReference,
    ErrorPolicy,
    ExecutionContext,
    HookDefinition,
    HookSet,
    NodeKind,
    PhaseDefinition,
    Status,
    StepDefinition,
    StepResult,
    WorkflowDefinition,
)


class FailExecutor(StepExecutor):
    step_type = "fail_for_policy"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        return StepResult.failed(step, "fallo intencional", "INTENTIONAL")


def test_from_step_follows_real_descendants_not_topological_suffix() -> None:
    workflow = WorkflowDefinition(
        "branches",
        [
            StepDefinition("root", "note"),
            StepDefinition("alpha", "note", needs=["root"]),
            StepDefinition("beta", "note", needs=["root"]),
            StepDefinition("alpha_end", "note", needs=["alpha"]),
        ],
    )
    preview = WorkflowEngine().preview(workflow, ExecutionContext(from_step="alpha"))
    assert preview.selected == ["alpha", "alpha_end"]
    assert preview.excluded == ["root", "beta"]


def test_include_needs_adds_only_required_ancestors() -> None:
    workflow = WorkflowDefinition(
        "branches",
        [
            StepDefinition("root", "note"),
            StepDefinition("alpha", "note", needs=["root"]),
            StepDefinition("beta", "note", needs=["root"]),
            StepDefinition("alpha_end", "note", needs=["alpha"]),
        ],
    )
    preview = WorkflowEngine().preview(
        workflow,
        ExecutionContext(only_steps=["alpha_end"], include_needs=True),
    )
    assert preview.selected == ["root", "alpha", "alpha_end"]
    assert preview.excluded == ["beta"]


def test_phase_and_block_filters_operate_on_compiled_plan() -> None:
    workflow = WorkflowDefinition(
        "groups",
        [
            StepDefinition("a", "note", phase="prepare", block="gimp"),
            StepDefinition("b", "note", phase="install", block="gimp"),
            StepDefinition("c", "note", phase="install", block="vlc"),
        ],
        phases={"prepare": PhaseDefinition(), "install": PhaseDefinition()},
    )
    engine = WorkflowEngine()
    assert engine.preview(workflow, ExecutionContext(phases=["install"])).selected == ["b", "c"]
    assert engine.preview(workflow, ExecutionContext(blocks=["gimp"])).selected == ["a", "b"]
    assert engine.preview(workflow, ExecutionContext(skip_blocks=["vlc"])).selected == ["a", "b"]


def test_error_policy_records_exact_provenance(tmp_path: Path) -> None:
    registry = ExecutorRegistry.default()
    registry.register(FailExecutor())
    workflow = WorkflowDefinition(
        "policy",
        [StepDefinition("verify", "fail_for_policy", phase="verify")],
        phases={"verify": PhaseDefinition()},
        on_error=ErrorPolicy(default="stop", phases={"verify": "warn"}),
    )
    run = WorkflowEngine(registry).run(workflow, ExecutionContext(root=tmp_path, dry_run=False))
    result = run.results[0]
    assert run.success is True
    assert result.status == Status.OK_WITH_WARNINGS
    assert result.data["policy"] == "warn"
    assert result.data["policy_source"] == "on_error.phases.verify"


def test_compiler_expands_checks_and_hooks_with_lineage() -> None:
    workflow = WorkflowDefinition(
        "composition",
        [
            StepDefinition(
                "install",
                "note",
                checks=CheckAttachment(after=[CheckReference("marker:ready")]),
            )
        ],
        hooks=HookSet(
            before_pipeline=[HookDefinition("inventory-before", "note")],
            after_pipeline=[HookDefinition("inventory-after", "note", run_if="always")],
        ),
    )
    plan = WorkflowEngine().compile(workflow)
    assert [node.kind for node in plan.nodes].count(NodeKind.ACTION) == 1
    assert [node.kind for node in plan.nodes].count(NodeKind.CHECK) == 1
    assert [node.kind for node in plan.nodes].count(NodeKind.HOOK) == 2
    check = next(node for node in plan.nodes if node.kind == NodeKind.CHECK)
    assert check.source_id == "install"
    assert check.generated is True
    assert plan.source_map[check.id].generated_from == "checks.after"


def test_fingerprints_are_stable_and_change_with_the_plan() -> None:
    engine = WorkflowEngine()
    first = WorkflowDefinition("fp", [StepDefinition("a", "note")])
    same = WorkflowDefinition("fp", [StepDefinition("a", "note")])
    changed = WorkflowDefinition("fp", [StepDefinition("a", "note", description="otro")])
    first_plan = engine.compile(first)
    same_plan = engine.compile(same)
    changed_plan = engine.compile(changed)
    assert first_plan.pipeline_fingerprint == same_plan.pipeline_fingerprint
    assert first_plan.plan_fingerprint == same_plan.plan_fingerprint
    assert first_plan.pipeline_fingerprint != changed_plan.pipeline_fingerprint
    assert first_plan.plan_fingerprint != changed_plan.plan_fingerprint


def test_run_writes_plan_events_and_explainable_report(tmp_path: Path) -> None:
    workflow = WorkflowDefinition("audit", [StepDefinition("hello", "note", phase="run", block="demo")])
    run = WorkflowEngine().run(workflow, ExecutionContext(root=tmp_path, dry_run=True))
    plan = json.loads(Path(run.plan_path).read_text(encoding="utf-8"))
    events = [json.loads(line) for line in Path(run.events_path).read_text(encoding="utf-8").splitlines()]
    report = json.loads(Path(run.report_path).read_text(encoding="utf-8"))
    assert plan["plan_version"] == "pipecraft.plan/v1.3"
    assert any(event["kind"] == "node_started" for event in events)
    assert any(event["kind"] == "node_finished" for event in events)
    assert report["pipeline_fingerprint"] == plan["pipeline_fingerprint"]
    assert report["plan_fingerprint"] == plan["plan_fingerprint"]
