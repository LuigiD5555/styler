from __future__ import annotations

import threading
import time
from pathlib import Path

from tests.support.local_engine import WorkflowEngine
from styler.execution.base import ExecutorRegistry, StepExecutor
from styler.planning.models import ErrorPolicy, ExecutionContext, Status, StepDefinition, StepResult, WorkflowDefinition


class ProbeExecutor(StepExecutor):
    def __init__(self):
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.calls: dict[str, int] = {}
        self.windows: dict[str, tuple[float, float]] = {}

    @property
    def step_type(self):
        return "probe"

    def run(self, step, ctx):
        start = time.monotonic()
        with self.lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            self.calls[step.id] = self.calls.get(step.id, 0) + 1
        time.sleep(float(step.config.get("sleep", 0.04)))
        with self.lock:
            self.running -= 1
        end = time.monotonic()
        self.windows[step.id] = (start, end)
        if step.config.get("fail"):
            return StepResult.failed(step, "fallo solicitado", "PROBE_FAILED")
        return StepResult(step.id, step.step_type, True, Status.OK, "ok")


def engine(executor):
    registry = ExecutorRegistry()
    registry.register(executor)
    return WorkflowEngine(registry)


def test_independent_steps_run_in_parallel(tmp_path: Path):
    probe = ProbeExecutor()
    workflow = WorkflowDefinition("parallel", [
        StepDefinition("a", "probe"),
        StepDefinition("b", "probe"),
    ])
    run = engine(probe).run(workflow, ExecutionContext(root=tmp_path, dry_run=True))
    assert run.success
    assert probe.max_running >= 2


def test_exclusive_apt_resource_serializes_steps(tmp_path: Path):
    probe = ProbeExecutor()
    workflow = WorkflowDefinition("apt-lock", [
        StepDefinition("a", "probe", exclusive_resources=["apt", "dpkg"]),
        StepDefinition("b", "probe", exclusive_resources=["apt", "dpkg"]),
    ])
    run = engine(probe).run(workflow, ExecutionContext(root=tmp_path, dry_run=True))
    assert run.success
    assert probe.max_running == 1


def test_failure_blocks_only_dependents(tmp_path: Path):
    probe = ProbeExecutor()
    workflow = WorkflowDefinition("branches", [
        StepDefinition("base", "probe", config={"fail": True}),
        StepDefinition("dependent", "probe", needs=["base"]),
        StepDefinition("independent", "probe"),
    ])
    run = engine(probe).run(workflow, ExecutionContext(root=tmp_path, dry_run=True))
    results = {r.step_id: r for r in run.results}
    assert results["base"].status == Status.FAILED
    assert results["dependent"].status == Status.BLOCKED
    assert results["independent"].success


def test_critical_failure_stops_remaining_plan(tmp_path: Path):
    probe = ProbeExecutor()
    workflow = WorkflowDefinition(
        "critical",
        [
            StepDefinition("critical", "probe", barrier=True, config={"fail": True}),
            StepDefinition("later", "probe", needs=["critical"]),
        ],
        on_error=ErrorPolicy(nodes={"critical": "stop"}),
    )
    run = engine(probe).run(workflow, ExecutionContext(root=tmp_path, dry_run=True))
    results = {r.step_id: r for r in run.results}
    assert not run.success
    assert results["later"].status == Status.BLOCKED


def test_resume_does_not_repeat_completed_steps(tmp_path: Path):
    probe = ProbeExecutor()
    workflow = WorkflowDefinition("resume", [StepDefinition("once", "probe")])
    ctx = ExecutionContext(root=tmp_path, dry_run=True, run_id="fixed-run")
    first = engine(probe).run(workflow, ctx)
    second = engine(probe).run(workflow, ctx)
    assert first.success and second.success
    assert probe.calls["once"] == 1
    assert second.results[0].data["resumed"] is True
