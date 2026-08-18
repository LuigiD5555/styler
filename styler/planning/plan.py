"""Compilador del plan semántico que Styler entrega a PipeCraft.

El compilador no ejecuta nada. Expande checks y hooks, conserva procedencia,
construye grupos de fases/bloques y produce fingerprints deterministas.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterable

from styler.planning.graph import topological_order
from styler.planning.models import (
    CheckReference,
    ExecutionPlan,
    HookDefinition,
    HookFilter,
    NodeKind,
    PlanGroup,
    PlanNode,
    RunCondition,
    SourceLocation,
    StepDefinition,
    WorkflowDefinition,
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _jsonable(to_dict())
        except Exception:
            pass
    if callable(value):
        return {
            "__callable__": f"{getattr(value, '__module__', '')}.{getattr(value, '__qualname__', value.__class__.__qualname__)}"
        }
    if hasattr(value, "__dict__"):
        return {
            "__class__": f"{value.__class__.__module__}.{value.__class__.__qualname__}",
            "state": _jsonable(vars(value)),
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {"__class__": f"{value.__class__.__module__}.{value.__class__.__qualname__}"}


def fingerprint(value: Any) -> str:
    """FNV-1a de 64 bits usado para fingerprints estables de planificación."""
    payload = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    result = 0xCBF29CE484222325
    for byte in payload:
        result ^= byte
        result = (result * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"fnv1a64:{result:016x}"


def _slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return text or "generated"


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _check_step(parent: StepDefinition, reference: CheckReference, node_id: str) -> StepDefinition:
    config = dict(reference.with_values)
    config.setdefault("checks", [reference.uses])
    return StepDefinition(
        id=node_id,
        source_id=parent.id,
        step_type="verify",
        description=f"Comprobar {reference.uses}",
        risk="low",
        requires_approval=False,
        required=parent.required if reference.required is None else bool(reference.required),
        config=config,
        phase=parent.phase,
        block=parent.block,
        tags=_dedupe([*parent.tags, "generated", "check"]),
        run_if=RunCondition.ALL_SUCCESS,
        kind=NodeKind.CHECK,
        provider=parent.provider,
        timeout=parent.timeout,
    )


def _hook_step(target: StepDefinition | None, hook: HookDefinition, node_id: str, *, phase: str = "", block: str = "") -> StepDefinition:
    return StepDefinition(
        id=node_id,
        source_id=hook.id,
        step_type=hook.step_type,
        description=hook.description or hook.id,
        required=False,
        config=dict(hook.with_values),
        phase=phase or (target.phase if target else ""),
        block=block or (target.block if target else ""),
        tags=_dedupe([*(target.tags if target else []), "generated", "hook"]),
        run_if=hook.run_if,
        kind=NodeKind.HOOK,
    )


def _filter_matches(step: StepDefinition, filter_: HookFilter) -> bool:
    tests: list[bool] = []
    if filter_.phases:
        tests.append(step.phase in filter_.phases)
    if filter_.blocks:
        tests.append(step.block in filter_.blocks)
    if filter_.steps:
        tests.append(step.id in filter_.steps or step.source_id in filter_.steps)
    if filter_.step_types:
        tests.append(step.step_type in filter_.step_types)
    if filter_.tags:
        tests.append(bool(set(filter_.tags) & set(step.tags)))
    return all(tests) if tests else True


def _hook_matches(step: StepDefinition, hook: HookDefinition, generated: bool = False) -> bool:
    if generated and not hook.include_generated:
        return False
    if not _filter_matches(step, hook.match):
        return False
    if hook.except_filter != HookFilter() and _filter_matches(step, hook.except_filter):
        return False
    return True


def compile_workflow(workflow: WorkflowDefinition) -> ExecutionPlan:
    """Compila un workflow fuente en un DAG ejecutable e inmutable."""
    source_steps = [replace(step, needs=list(step.needs), tags=list(step.tags), config=dict(step.config)) for step in workflow.steps]
    nodes: list[PlanNode] = []
    source_map: dict[str, SourceLocation] = {}
    lineage: list[dict[str, Any]] = []

    for step in source_steps:
        nodes.append(
            PlanNode(
                id=step.id,
                kind=step.kind,
                source_id=step.source_id or step.id,
                step=step,
                phase=step.phase,
                block=step.block,
                tags=list(step.tags),
                needs=list(step.needs),
                run_if=step.run_if,
                generated=False,
            )
        )
        source_map[step.id] = SourceLocation(source_id=step.source_id or step.id, generated_from="source")

    # Checks adjuntos: antes, después y al fallar. Los checks posteriores pasan
    # a ser la garantía de salida del paso, por lo que se recablean dependientes.
    for source in source_steps:
        action = next(node for node in nodes if node.id == source.id)
        before_tail: str | None = None
        for index, reference in enumerate(source.checks.before, 1):
            node_id = f"{source.id}__check_before_{index}_{_slug(reference.uses)}"
            check_step = _check_step(source, reference, node_id)
            check_needs = list(source.needs) if before_tail is None else [before_tail]
            nodes.append(PlanNode(
                id=node_id, kind=NodeKind.CHECK, source_id=source.id, step=check_step,
                phase=source.phase, block=source.block, tags=list(check_step.tags),
                needs=check_needs, run_if=RunCondition.ALL_SUCCESS, generated=True,
                on_failure=reference.on_failure, severity=reference.severity,
                standalone=reference.standalone,
            ))
            source_map[node_id] = SourceLocation(source_id=source.id, generated_from="checks.before")
            lineage.append({"node_id": node_id, "source_id": source.id, "generated_from": "checks.before"})
            before_tail = node_id
        if before_tail:
            action.needs = [before_tail]
            action.step.needs = [before_tail]

        after_tail = source.id
        generated_after: list[str] = []
        for index, reference in enumerate(source.checks.after, 1):
            node_id = f"{source.id}__check_after_{index}_{_slug(reference.uses)}"
            check_step = _check_step(source, reference, node_id)
            nodes.append(PlanNode(
                id=node_id, kind=NodeKind.CHECK, source_id=source.id, step=check_step,
                phase=source.phase, block=source.block, tags=list(check_step.tags),
                needs=[after_tail], run_if=RunCondition.ALL_SUCCESS, generated=True,
                on_failure=reference.on_failure, severity=reference.severity,
                standalone=reference.standalone,
            ))
            source_map[node_id] = SourceLocation(source_id=source.id, generated_from="checks.after")
            lineage.append({"node_id": node_id, "source_id": source.id, "generated_from": "checks.after"})
            after_tail = node_id
            generated_after.append(node_id)
        if generated_after:
            for candidate in nodes:
                if candidate.id in generated_after or candidate.id == source.id:
                    continue
                if source.id in candidate.needs:
                    candidate.needs = [after_tail if dep == source.id else dep for dep in candidate.needs]
                    candidate.step.needs = list(candidate.needs)

        for index, reference in enumerate(source.checks.on_failure, 1):
            node_id = f"{source.id}__check_failure_{index}_{_slug(reference.uses)}"
            check_step = _check_step(source, reference, node_id)
            check_step.run_if = RunCondition.ANY_FAILED
            nodes.append(PlanNode(
                id=node_id, kind=NodeKind.CHECK, source_id=source.id, step=check_step,
                phase=source.phase, block=source.block, tags=list(check_step.tags),
                needs=[source.id], run_if=RunCondition.ANY_FAILED, generated=True,
                on_failure=reference.on_failure, severity=reference.severity,
                standalone=reference.standalone,
            ))
            source_map[node_id] = SourceLocation(source_id=source.id, generated_from="checks.on_failure")
            lineage.append({"node_id": node_id, "source_id": source.id, "generated_from": "checks.on_failure"})

    # Hooks por paso. before_step se intercala delante; after_step se convierte
    # en salida; on_step_failure conserva una rama de recuperación.
    for source in source_steps:
        target = next(node for node in nodes if node.id == source.id)
        before_tail: str | None = None
        for index, hook in enumerate(workflow.hooks.before_step, 1):
            if not _hook_matches(source, hook):
                continue
            node_id = f"{source.id}__hook_before_{index}_{_slug(hook.id)}"
            hook_step = _hook_step(source, hook, node_id)
            needs = list(target.needs) if before_tail is None else [before_tail]
            nodes.append(PlanNode(node_id, NodeKind.HOOK, hook.id, hook_step, source.phase, source.block,
                                  list(hook_step.tags), needs, hook.run_if, True))
            source_map[node_id] = SourceLocation(source_id=hook.id, generated_from="hooks.before_step")
            lineage.append({"node_id": node_id, "source_id": hook.id, "target": source.id, "generated_from": "hooks.before_step"})
            before_tail = node_id
        if before_tail:
            target.needs = [before_tail]
            target.step.needs = [before_tail]

        after_tail = source.id
        generated_after: list[str] = []
        for index, hook in enumerate(workflow.hooks.after_step, 1):
            if not _hook_matches(source, hook):
                continue
            node_id = f"{source.id}__hook_after_{index}_{_slug(hook.id)}"
            hook_step = _hook_step(source, hook, node_id)
            nodes.append(PlanNode(node_id, NodeKind.HOOK, hook.id, hook_step, source.phase, source.block,
                                  list(hook_step.tags), [after_tail], hook.run_if, True))
            source_map[node_id] = SourceLocation(source_id=hook.id, generated_from="hooks.after_step")
            lineage.append({"node_id": node_id, "source_id": hook.id, "target": source.id, "generated_from": "hooks.after_step"})
            after_tail = node_id
            generated_after.append(node_id)
        if generated_after:
            for candidate in nodes:
                if candidate.id in generated_after or candidate.id == source.id:
                    continue
                if source.id in candidate.needs:
                    candidate.needs = [after_tail if dep == source.id else dep for dep in candidate.needs]
                    candidate.step.needs = list(candidate.needs)

        for index, hook in enumerate(workflow.hooks.on_step_failure, 1):
            if not _hook_matches(source, hook):
                continue
            node_id = f"{source.id}__hook_failure_{index}_{_slug(hook.id)}"
            hook_step = _hook_step(source, hook, node_id)
            hook_step.run_if = RunCondition.ANY_FAILED
            nodes.append(PlanNode(node_id, NodeKind.HOOK, hook.id, hook_step, source.phase, source.block,
                                  list(hook_step.tags), [source.id], RunCondition.ANY_FAILED, True))
            source_map[node_id] = SourceLocation(source_id=hook.id, generated_from="hooks.on_step_failure")
            lineage.append({"node_id": node_id, "source_id": hook.id, "target": source.id, "generated_from": "hooks.on_step_failure"})

    # Hooks globales. Se colocan al inicio o después de todos los nodos terminales.
    roots = [node.id for node in nodes if not node.needs]
    before_pipeline_tail: str | None = None
    for index, hook in enumerate(workflow.hooks.before_pipeline, 1):
        node_id = f"__pipeline__hook_before_{index}_{_slug(hook.id)}"
        hook_step = _hook_step(None, hook, node_id)
        needs = [] if before_pipeline_tail is None else [before_pipeline_tail]
        nodes.append(PlanNode(node_id, NodeKind.HOOK, hook.id, hook_step, tags=list(hook_step.tags),
                              needs=needs, run_if=hook.run_if, generated=True))
        source_map[node_id] = SourceLocation(source_id=hook.id, generated_from="hooks.before_pipeline")
        before_pipeline_tail = node_id
    if before_pipeline_tail:
        for node in nodes:
            if node.id in roots:
                node.needs = [before_pipeline_tail]
                node.step.needs = [before_pipeline_tail]

    all_ids = {node.id for node in nodes}
    depended = {dependency for node in nodes for dependency in node.needs if dependency in all_ids}
    terminals = [node.id for node in nodes if node.id not in depended]
    after_pipeline_tail: str | None = None
    for index, hook in enumerate(workflow.hooks.after_pipeline, 1):
        node_id = f"__pipeline__hook_after_{index}_{_slug(hook.id)}"
        hook_step = _hook_step(None, hook, node_id)
        needs = terminals if after_pipeline_tail is None else [after_pipeline_tail]
        nodes.append(PlanNode(node_id, NodeKind.HOOK, hook.id, hook_step, tags=list(hook_step.tags),
                              needs=needs, run_if=hook.run_if, generated=True))
        source_map[node_id] = SourceLocation(source_id=hook.id, generated_from="hooks.after_pipeline")
        after_pipeline_tail = node_id

    plan = ExecutionPlan(
        pipeline=workflow.name,
        nodes=nodes,
        dependency_mode=workflow.dependency_mode,
        source_map=source_map,
        lineage=lineage,
    )
    # Validar ciclo antes de calcular grupos/fingerprints.
    topological_order(plan)

    phases: dict[str, PlanGroup] = {}
    blocks: dict[str, PlanGroup] = {}
    for node in nodes:
        if node.phase:
            definition = workflow.phases.get(node.phase)
            group = phases.setdefault(node.phase, PlanGroup(node.phase))
            if definition:
                group.description = definition.description
                group.tags = list(definition.tags)
            group.nodes.append(node.id)
        if node.block:
            blocks.setdefault(node.block, PlanGroup(node.block)).nodes.append(node.id)
    plan.phases = list(phases.values())
    plan.blocks = list(blocks.values())
    plan.pipeline_fingerprint = fingerprint(workflow_to_dict(workflow))
    plan.plan_fingerprint = fingerprint(plan_to_dict(plan, include_fingerprints=False))
    return plan


def workflow_to_dict(workflow: WorkflowDefinition) -> dict[str, Any]:
    return _jsonable(asdict(workflow))


def plan_to_dict(plan: ExecutionPlan, *, include_fingerprints: bool = True) -> dict[str, Any]:
    payload = _jsonable(asdict(plan))
    if not include_fingerprints:
        payload["pipeline_fingerprint"] = ""
        payload["plan_fingerprint"] = ""
    return payload
