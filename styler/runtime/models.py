"""Contrato canónico de planes y ejecuciones de Styler 0.5.

El modelo adopta las ideas y la semántica de PipeCraft 1.3.1 como contrato
interno: un pipeline declarativo se compila a un plan de nodos estable antes
de ejecutarse. Styler añade únicamente extensiones de dominio necesarias para
Linux: recursos concurrentes, capacidades, métodos, recibos y rollback.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any


class WorkflowOperation:
    GENERIC = "generic"
    APPLY = "apply"
    UNDO = "undo"
    INSPECT = "inspect"

    ALL = {GENERIC, APPLY, UNDO, INSPECT}


class NodeKind:
    ACTION = "action"
    CHECK = "check"
    HOOK = "hook"
    ALL = {ACTION, CHECK, HOOK}


class RunCondition:
    ALL_SUCCESS = "all_success"
    ALL_COMPLETE = "all_complete"
    ANY_FAILED = "any_failed"
    ALWAYS = "always"
    ALL = {ALL_SUCCESS, ALL_COMPLETE, ANY_FAILED, ALWAYS}


class DependencyMode:
    STRICT = "strict"
    ALL = {STRICT}


class Status:
    OK = "ok"
    OK_WITH_WARNINGS = "ok_with_warnings"
    RECONCILED = "reconciled"
    SKIPPED = "skipped"
    FAILED = "failed"
    NEEDS_APPROVAL = "needs_approval"
    DRY_RUN = "dry_run"
    PLANNED = "planned"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    ROLLED_BACK = "rolled_back"
    WAITING_FOR_USER = "waiting_for_user"
    REQUIRES_REBOOT = "requires_reboot"
    REQUIRES_LOGOUT = "requires_logout"

    ALL = {
        OK,
        OK_WITH_WARNINGS,
        RECONCILED,
        SKIPPED,
        FAILED,
        NEEDS_APPROVAL,
        DRY_RUN,
        PLANNED,
        TIMEOUT,
        CANCELLED,
        PENDING,
        READY,
        RUNNING,
        SUCCEEDED,
        BLOCKED,
        ROLLED_BACK,
        WAITING_FOR_USER,
        REQUIRES_REBOOT,
        REQUIRES_LOGOUT,
    }

    SUCCESS = {OK, OK_WITH_WARNINGS, RECONCILED, DRY_RUN, PLANNED, SUCCEEDED, ROLLED_BACK}
    TERMINAL = SUCCESS | {
        SKIPPED,
        FAILED,
        NEEDS_APPROVAL,
        TIMEOUT,
        CANCELLED,
        BLOCKED,
        WAITING_FOR_USER,
        REQUIRES_REBOOT,
        REQUIRES_LOGOUT,
    }


@dataclass
class CheckReference:
    uses: str
    with_values: dict[str, Any] = field(default_factory=dict)
    required: bool | None = None
    severity: str = "error"
    on_failure: str = "stop"
    standalone: bool = False


@dataclass
class CheckAttachment:
    before: list[CheckReference] = field(default_factory=list)
    after: list[CheckReference] = field(default_factory=list)
    on_failure: list[CheckReference] = field(default_factory=list)


@dataclass
class HookFilter:
    phases: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    step_types: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class HookDefinition:
    id: str
    step_type: str = "note"
    description: str = ""
    with_values: dict[str, Any] = field(default_factory=dict)
    run_if: str = RunCondition.ALL_SUCCESS
    include_generated: bool = False
    match: HookFilter = field(default_factory=HookFilter)
    except_filter: HookFilter = field(default_factory=HookFilter)


@dataclass
class HookSet:
    before_pipeline: list[HookDefinition] = field(default_factory=list)
    after_pipeline: list[HookDefinition] = field(default_factory=list)
    before_phase: list[HookDefinition] = field(default_factory=list)
    after_phase: list[HookDefinition] = field(default_factory=list)
    before_block: list[HookDefinition] = field(default_factory=list)
    after_block: list[HookDefinition] = field(default_factory=list)
    before_step: list[HookDefinition] = field(default_factory=list)
    after_step: list[HookDefinition] = field(default_factory=list)
    on_step_failure: list[HookDefinition] = field(default_factory=list)
    on_block_failure: list[HookDefinition] = field(default_factory=list)


@dataclass
class PhaseDefinition:
    description: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class StepDefinition:
    id: str
    step_type: str
    description: str = ""
    needs: list[str] = field(default_factory=list)
    risk: str = "low"
    requires_approval: bool = False
    required: bool = True
    config: dict[str, Any] = field(default_factory=dict)

    # PipeCraft 1.3.1 composition and explainability.
    phase: str = ""
    block: str = ""
    tags: list[str] = field(default_factory=list)
    run_if: str = RunCondition.ALL_SUCCESS
    checks: CheckAttachment = field(default_factory=CheckAttachment)
    observe: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    kind: str = NodeKind.ACTION
    source_id: str = ""

    # Styler execution extensions.
    retries: int = 0
    retry_delay: float = 0.0
    timeout: float | None = None
    requires: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    exclusive_resources: list[str] = field(default_factory=list)
    shared_resources: list[str] = field(default_factory=list)
    barrier: bool = False
    provider: str = ""
    session_support: dict[str, str] = field(default_factory=dict)
    rollback: dict[str, Any] = field(default_factory=dict)
    operation: str = ""
    method_id: str = ""
    method_reason: str = ""
    method_candidates: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.source_id:
            self.source_id = self.id


@dataclass
class ErrorPolicy:
    default: str = "stop"
    nodes: dict[str, str] = field(default_factory=dict)
    steps: dict[str, str] = field(default_factory=dict)
    blocks: dict[str, str] = field(default_factory=dict)
    phases: dict[str, str] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)

    def resolve(self, node: "PlanNode", status: str) -> tuple[str, str]:
        candidates = (
            (self.nodes, node.id, "on_error.nodes"),
            (self.steps, node.source_id, "on_error.steps"),
            (self.blocks, node.block, "on_error.blocks"),
            (self.phases, node.phase, "on_error.phases"),
            (self.types, node.step.step_type, "on_error.types"),
            (self.statuses, status, "on_error.statuses"),
        )
        for mapping, key, source in candidates:
            if key and key in mapping:
                return mapping[key], f"{source}.{key}"
        return self.default, "on_error.default"


@dataclass
class WorkflowDefinition:
    name: str
    steps: list[StepDefinition]
    description: str = ""
    operation: str = WorkflowOperation.GENERIC
    metadata: dict[str, Any] = field(default_factory=dict)
    on_error: ErrorPolicy = field(default_factory=ErrorPolicy)
    phases: dict[str, PhaseDefinition] = field(default_factory=dict)
    hooks: HookSet = field(default_factory=HookSet)
    dependency_mode: str = DependencyMode.STRICT
    observations: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "pipecraft/v1"


@dataclass
class SourceLocation:
    source_file: str = ""
    source_id: str = ""
    generated_from: str = ""


@dataclass
class PlanNode:
    id: str
    kind: str
    source_id: str
    step: StepDefinition
    phase: str = ""
    block: str = ""
    tags: list[str] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)
    run_if: str = RunCondition.ALL_SUCCESS
    generated: bool = False
    on_failure: str = ""
    severity: str = ""
    standalone: bool = False


@dataclass
class PlanGroup:
    id: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)


@dataclass
class ExecutionPlan:
    pipeline: str
    nodes: list[PlanNode]
    dependency_mode: str = DependencyMode.STRICT
    phases: list[PlanGroup] = field(default_factory=list)
    blocks: list[PlanGroup] = field(default_factory=list)
    lineage: list[dict[str, Any]] = field(default_factory=list)
    source_map: dict[str, SourceLocation] = field(default_factory=dict)
    plan_version: str = "pipecraft.plan/v1.3"
    pipeline_fingerprint: str = ""
    plan_fingerprint: str = ""

    def node(self, node_id: str) -> PlanNode | None:
        return next((node for node in self.nodes if node.id == node_id), None)


@dataclass
class ExecutionContext:
    root: Path = field(default_factory=lambda: Path("."))
    dry_run: bool = True
    approve: bool = False
    labels: list[str] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)
    runs_dir: str = ".styler/runs"
    run_id: str = ""

    # PipeCraft 1.3.1 structural selection.
    from_step: str | None = None
    downstream_of: str | None = None
    upstream_of: str | None = None
    only_steps: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    skip_blocks: list[str] = field(default_factory=list)
    include_needs: bool = False
    preview: bool = False
    checks_mode: str = "defaults"

    run_dir: Path = field(default_factory=Path)
    artifacts_dir: Path = field(default_factory=Path)
    logs_dir: Path = field(default_factory=Path)
    events_path: Path = field(default_factory=Path)
    plan_path: Path = field(default_factory=Path)

    def for_run(self, run_id: str) -> "ExecutionContext":
        run_dir = self.root / self.runs_dir / run_id
        return replace(
            self,
            run_id=run_id,
            run_dir=run_dir,
            artifacts_dir=run_dir / "artifacts",
            logs_dir=run_dir / "logs",
            events_path=run_dir / "events.jsonl",
            plan_path=run_dir / "plan.json",
        )


@dataclass
class StepResult:
    step_id: str
    step_type: str
    success: bool
    status: str
    message: str
    output: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    node_id: str = ""
    source_step_id: str = ""
    node_kind: str = NodeKind.ACTION
    phase: str = ""
    block: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    attempts: int = 1
    queue_ms: int = 0
    blocked_ms: int = 0

    def __post_init__(self) -> None:
        if not self.node_id:
            self.node_id = self.step_id
        if not self.source_step_id:
            self.source_step_id = self.step_id

    @classmethod
    def failed(
        cls,
        step: StepDefinition,
        message: str,
        code: str,
        hint: str = "",
    ) -> "StepResult":
        data: dict[str, Any] = {"error_code": code}
        if hint:
            data["hint"] = hint
        return cls(step.id, step.step_type, False, Status.FAILED, message, data=data)

    def with_node(self, node: PlanNode) -> "StepResult":
        self.node_id = node.id
        self.step_id = node.id
        self.source_step_id = node.source_id
        self.node_kind = node.kind
        self.phase = node.phase
        self.block = node.block
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PipelineSummary:
    actions: int = 0
    checks: int = 0
    hooks: int = 0
    succeeded: int = 0
    failed: int = 0
    blocked: int = 0
    skipped: int = 0
    warnings: int = 0


@dataclass
class WorkflowRun:
    run_id: str
    workflow: str
    success: bool
    dry_run: bool
    started_at: str
    finished_at: str
    results: list[StepResult]
    operation: str = WorkflowOperation.GENERIC
    status: str = ""
    order: list[str] = field(default_factory=list)
    pipeline_fingerprint: str = ""
    plan_fingerprint: str = ""
    summary: PipelineSummary = field(default_factory=PipelineSummary)
    selected_from: str = ""
    selected_downstream_of: str = ""
    selected_upstream_of: str = ""
    selected_only: list[str] = field(default_factory=list)
    selected_phases: list[str] = field(default_factory=list)
    selected_blocks: list[str] = field(default_factory=list)
    skipped_blocks: list[str] = field(default_factory=list)
    run_dir: str = ""
    artifacts_dir: str = ""
    logs_dir: str = ""
    events_path: str = ""
    plan_path: str = ""
    report_path: str = ""
    trace_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["results"] = [result.to_dict() for result in self.results]
        return value
