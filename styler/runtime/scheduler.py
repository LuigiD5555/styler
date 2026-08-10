"""Scheduler concurrente sobre planes PipeCraft 1.3.1.

Combina la semántica de dependencias, condiciones y políticas de PipeCraft con
las extensiones de Styler: recursos, barreras, capacidades y reanudación.
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from styler.runtime.events import EventWriter
from styler.runtime.graph import topological_order
from styler.runtime.models import (
    DependencyMode,
    ErrorPolicy,
    ExecutionPlan,
    PlanNode,
    RunCondition,
    Status,
    StepResult,
)


@dataclass
class SchedulerState:
    plan_fingerprint: str = ""
    statuses: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    results: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "plan_fingerprint": self.plan_fingerprint,
            "statuses": self.statuses,
            "reasons": self.reasons,
            "results": self.results,
        }

    @classmethod
    def load(cls, path: Path) -> "SchedulerState":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return cls()
        return cls(
            plan_fingerprint=str(data.get("plan_fingerprint", "")),
            statuses=dict(data.get("statuses", {})),
            reasons=dict(data.get("reasons", {})),
            results=dict(data.get("results", {})),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)


class ResourceTable:
    def __init__(self) -> None:
        self._exclusive: set[str] = set()
        self._shared: dict[str, int] = {}
        self._lock = threading.Lock()

    def can_acquire(self, node: PlanNode) -> bool:
        step = node.step
        with self._lock:
            if any(resource in self._exclusive or self._shared.get(resource, 0) for resource in step.exclusive_resources):
                return False
            if any(resource in self._exclusive for resource in step.shared_resources):
                return False
            return True

    def acquire(self, node: PlanNode) -> None:
        step = node.step
        with self._lock:
            self._exclusive.update(step.exclusive_resources)
            for resource in step.shared_resources:
                self._shared[resource] = self._shared.get(resource, 0) + 1

    def release(self, node: PlanNode) -> None:
        step = node.step
        with self._lock:
            self._exclusive.difference_update(step.exclusive_resources)
            for resource in step.shared_resources:
                remaining = self._shared.get(resource, 0) - 1
                if remaining > 0:
                    self._shared[resource] = remaining
                else:
                    self._shared.pop(resource, None)


def _result_from_dict(raw: dict) -> StepResult:
    allowed = StepResult.__dataclass_fields__
    return StepResult(**{key: value for key, value in raw.items() if key in allowed})


def _terminal(status: str) -> bool:
    return status in Status.TERMINAL


def _success(status: str) -> bool:
    return status in Status.SUCCESS


def _failure(status: str) -> bool:
    return _terminal(status) and not _success(status) and status != Status.SKIPPED


def _synthetic(node: PlanNode, status: str, success: bool, message: str, **data: object) -> StepResult:
    return StepResult(
        node.id,
        node.step.step_type,
        success,
        status,
        message,
        data=dict(data),
    ).with_node(node)


def _blocking_dependencies(node: PlanNode, state: SchedulerState) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for dependency in node.needs:
        status = state.statuses.get(dependency, Status.BLOCKED)
        if _success(status):
            continue
        values.append({
            "node_id": dependency,
            "state": status,
            "reason": state.reasons.get(dependency, ""),
        })
    return values


def schedule(
    plan: ExecutionPlan,
    selected: set[str],
    execute: Callable[[PlanNode], StepResult],
    state_path: Path,
    *,
    policy: ErrorPolicy,
    events: EventWriter,
    max_workers: int = 4,
    resume: bool = True,
) -> tuple[list[StepResult], bool, str]:
    order = topological_order(plan)
    by_id = {node.id: node for node in plan.nodes}
    state = SchedulerState.load(state_path) if resume else SchedulerState()
    if state.plan_fingerprint != plan.plan_fingerprint:
        state = SchedulerState(plan_fingerprint=plan.plan_fingerprint)
    else:
        state.plan_fingerprint = plan.plan_fingerprint

    results: list[StepResult] = []
    result_by_id: dict[str, StepResult] = {}
    resources = ResourceTable()
    running: dict[Future[StepResult], PlanNode] = {}
    stop_requested = False
    hard_failure = False
    warnings = False

    for node in plan.nodes:
        if node.id not in selected:
            state.statuses[node.id] = Status.SKIPPED
            state.reasons[node.id] = "selection_excluded"
            result = _synthetic(node, Status.SKIPPED, True, "Excluido por la selección estructural.", reason="selection_excluded")
            state.results[node.id] = result.to_dict()
            results.append(result)
            result_by_id[node.id] = result
            events.emit("node_skipped", node=node, result=result, data={"reason": "selection_excluded"})
            continue

        previous = state.statuses.get(node.id)
        if resume and previous in Status.SUCCESS and node.id in state.results:
            prior = _result_from_dict(state.results[node.id])
            prior.data["resumed"] = True
            prior.with_node(node)
            results.append(prior)
            result_by_id[node.id] = prior
            events.emit("node_resumed", node=node, result=prior, data={"status": prior.status})
        else:
            state.statuses[node.id] = Status.PENDING
            state.reasons.pop(node.id, None)
            state.results.pop(node.id, None)
    state.save(state_path)

    started_phases: set[str] = set()
    started_blocks: set[str] = set()
    finished_phases: set[str] = set()
    finished_blocks: set[str] = set()

    def emit_group_starts(node: PlanNode) -> None:
        if node.phase and node.phase not in started_phases:
            started_phases.add(node.phase)
            events.emit("phase_started", node=node, data={"phase": node.phase})
        if node.block and node.block not in started_blocks:
            started_blocks.add(node.block)
            events.emit("block_started", node=node, data={"block": node.block})

    def emit_group_finishes() -> None:
        for group in plan.phases:
            if group.id in finished_phases:
                continue
            if all(_terminal(state.statuses.get(node_id, Status.PENDING)) for node_id in group.nodes):
                finished_phases.add(group.id)
                node = next((by_id[item] for item in group.nodes if item in by_id), None)
                events.emit("phase_finished", node=node, data={"phase": group.id})
        for group in plan.blocks:
            if group.id in finished_blocks:
                continue
            if all(_terminal(state.statuses.get(node_id, Status.PENDING)) for node_id in group.nodes):
                finished_blocks.add(group.id)
                node = next((by_id[item] for item in group.nodes if item in by_id), None)
                events.emit("block_finished", node=node, data={"block": group.id})

    def persist_result(node: PlanNode, result: StepResult, reason: str = "") -> None:
        nonlocal hard_failure, warnings, stop_requested
        result.with_node(node)
        failed_before_policy = not result.success
        resolved_policy, policy_source = policy.resolve(node, result.status)

        if failed_before_policy and not node.step.required:
            result.data.setdefault("original_status", result.status)
            result.success = True
            result.status = Status.OK_WITH_WARNINGS
            result.message = f"Advertencia opcional: {result.message}"
            resolved_policy = "warn"
            policy_source = "node.required=false"
            warnings = True
        elif failed_before_policy:
            if resolved_policy == "warn":
                result.data.setdefault("original_status", result.status)
                result.success = True
                result.status = Status.OK_WITH_WARNINGS
                result.message = f"Advertencia: {result.message}"
                warnings = True
            elif resolved_policy == "continue":
                hard_failure = True
            else:
                hard_failure = True
                stop_requested = True

        if result.status == Status.OK_WITH_WARNINGS:
            warnings = True
        result.data["policy"] = resolved_policy
        result.data["policy_source"] = policy_source
        if reason:
            result.data.setdefault("reason", reason)
        result.data.setdefault("resources", {
            "exclusive": list(node.step.exclusive_resources),
            "shared": list(node.step.shared_resources),
        })
        state.statuses[node.id] = result.status if _terminal(result.status) else (Status.SUCCEEDED if result.success else Status.FAILED)
        state.reasons[node.id] = reason or ("completed" if result.success else "execution_failed")
        state.results[node.id] = result.to_dict()
        state.save(state_path)
        results.append(result)
        result_by_id[node.id] = result
        events.emit("node_finished", node=node, result=result, data={
            "success": result.success,
            "status": result.status,
            "message": result.message,
            "policy": resolved_policy,
            "policy_source": policy_source,
            "error_code": result.data.get("error_code"),
        })
        emit_group_finishes()

    def mark_synthetic(node: PlanNode, status: str, success: bool, message: str, reason: str, **data: object) -> None:
        result = _synthetic(node, status, success, message, reason=reason, **data)
        state.statuses[node.id] = status
        state.reasons[node.id] = reason
        state.results[node.id] = result.to_dict()
        state.save(state_path)
        results.append(result)
        result_by_id[node.id] = result
        events.emit("node_skipped" if status == Status.SKIPPED else "node_blocked", node=node, result=result, data={"reason": reason, **data})
        emit_group_finishes()

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        while True:
            changed = False

            # Resolver condiciones y bloqueos cuyos requisitos ya terminaron.
            for node_id in order:
                node = by_id[node_id]
                if node.id not in selected or state.statuses.get(node.id) != Status.PENDING:
                    continue
                dependency_statuses = [state.statuses.get(dep, Status.PENDING) for dep in node.needs]
                if not all(_terminal(value) for value in dependency_statuses):
                    continue

                if stop_requested and node.run_if not in {RunCondition.ALWAYS, RunCondition.ANY_FAILED}:
                    mark_synthetic(
                        node,
                        Status.BLOCKED,
                        False,
                        "Bloqueado porque el pipeline se detuvo tras un fallo.",
                        "pipeline_stopped",
                        blocking_dependencies=_blocking_dependencies(node, state),
                    )
                    hard_failure = hard_failure or node.step.required
                    changed = True
                    continue

                if node.run_if == RunCondition.ALL_SUCCESS:
                    if all(_success(value) for value in dependency_statuses):
                        state.statuses[node.id] = Status.READY
                        state.save(state_path)
                        changed = True
                    else:
                        reason = "dependency_skipped" if any(value == Status.SKIPPED for value in dependency_statuses) else "dependency_failed"
                        mark_synthetic(
                            node,
                            Status.BLOCKED,
                            False,
                            "Bloqueado porque una dependencia no terminó correctamente.",
                            reason,
                            blocking_dependencies=_blocking_dependencies(node, state),
                        )
                        hard_failure = hard_failure or node.step.required
                        changed = True
                elif node.run_if in {RunCondition.ALL_COMPLETE, RunCondition.ALWAYS}:
                    state.statuses[node.id] = Status.READY
                    state.save(state_path)
                    changed = True
                elif node.run_if == RunCondition.ANY_FAILED:
                    if any(_failure(value) for value in dependency_statuses):
                        state.statuses[node.id] = Status.READY
                        state.save(state_path)
                    else:
                        mark_synthetic(
                            node,
                            Status.SKIPPED,
                            True,
                            "La condición any_failed no se cumplió.",
                            "condition_not_met",
                        )
                    changed = True

            running_nodes = set(node.id for node in running.values())
            barrier_running = any(node.step.barrier for node in running.values())
            provided = {
                capability
                for result in result_by_id.values()
                if result.success and result.node_id in by_id
                for capability in by_id[result.node_id].step.provides
            }

            for node_id in order:
                node = by_id[node_id]
                if node.id not in selected or node.id in running_nodes or state.statuses.get(node.id) != Status.READY:
                    continue

                missing = [capability for capability in node.step.requires if capability not in provided]
                if missing:
                    possible = {
                        capability
                        for candidate in plan.nodes
                        if candidate.id in selected and not _terminal(state.statuses.get(candidate.id, Status.PENDING))
                        for capability in candidate.step.provides
                    }
                    if any(capability in possible for capability in missing):
                        continue
                    mark_synthetic(
                        node,
                        Status.BLOCKED,
                        False,
                        "No se proporcionaron las capacidades requeridas.",
                        "missing_capabilities",
                        missing_capabilities=missing,
                    )
                    hard_failure = hard_failure or node.step.required
                    changed = True
                    continue

                if barrier_running or (node.step.barrier and running):
                    continue
                if not resources.can_acquire(node):
                    continue

                resources.acquire(node)
                state.statuses[node.id] = Status.RUNNING
                state.reasons[node.id] = "running"
                state.save(state_path)
                emit_group_starts(node)
                events.emit("node_started", node=node, data={
                    "run_if": node.run_if,
                    "generated": node.generated,
                    "needs": list(node.needs),
                })
                future = pool.submit(execute, node)
                running[future] = node
                changed = True
                if node.step.barrier:
                    break

            if running:
                done, _ = wait(tuple(running), return_when=FIRST_COMPLETED)
                for future in done:
                    node = running.pop(future)
                    resources.release(node)
                    try:
                        result = future.result()
                    except Exception as exc:  # protección adicional alrededor del callback
                        result = StepResult.failed(node.step, f"Error inesperado del scheduler: {exc}", "SCHEDULER_EXECUTION_ERROR")
                    persist_result(node, result)
                continue

            pending = [node for node in plan.nodes if node.id in selected and state.statuses.get(node.id) in {Status.PENDING, Status.READY, Status.RUNNING}]
            if not pending:
                break
            if not changed:
                for node in pending:
                    mark_synthetic(
                        node,
                        Status.BLOCKED,
                        False,
                        "El scheduler no encontró una transición válida.",
                        "scheduler_stalled",
                        blocking_dependencies=_blocking_dependencies(node, state),
                    )
                    hard_failure = hard_failure or node.step.required
                break

    # Reporte siempre en orden topológico, incluidos excluidos y generados.
    position = {node_id: index for index, node_id in enumerate(order)}
    results.sort(key=lambda result: position.get(result.node_id, len(position)))
    status = "failed" if hard_failure else ("success_with_warnings" if warnings else "success")
    return results, not hard_failure, status
