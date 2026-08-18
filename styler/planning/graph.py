"""Operaciones deterministas sobre DAGs de planes compilados."""

from __future__ import annotations

from collections import deque
from dataclasses import replace

from styler.planning.models import ExecutionPlan, StepDefinition, WorkflowDefinition


class DependencyCycleError(ValueError):
    def __init__(self, remaining: list[str]):
        self.remaining = remaining
        super().__init__(f"Ciclo de dependencias entre nodos: {', '.join(remaining)}")


def topological_order(items: list[StepDefinition] | ExecutionPlan) -> list[str]:
    """Kahn estable para pasos fuente o nodos de un plan compilado."""
    if isinstance(items, ExecutionPlan):
        ids = [node.id for node in items.nodes]
        needs_by_id = {node.id: list(node.needs) for node in items.nodes}
    else:
        ids = [step.id for step in items]
        needs_by_id = {step.id: list(step.needs) for step in items}

    known = set(ids)
    index = {item_id: pos for pos, item_id in enumerate(ids)}
    indegree = {item_id: 0 for item_id in ids}
    dependents: dict[str, list[str]] = {}

    for item_id in ids:
        for dependency in needs_by_id[item_id]:
            if dependency not in known:
                continue
            indegree[item_id] += 1
            dependents.setdefault(dependency, []).append(item_id)

    queue = deque(sorted((item_id for item_id in ids if indegree[item_id] == 0), key=index.get))
    ordered: list[str] = []
    while queue:
        item_id = queue.popleft()
        ordered.append(item_id)
        newly_ready: list[str] = []
        for child in dependents.get(item_id, []):
            indegree[child] -= 1
            if indegree[child] == 0:
                newly_ready.append(child)
        queue.extend(sorted(newly_ready, key=index.get))

    if len(ordered) != len(ids):
        remaining = sorted(item_id for item_id in ids if item_id not in ordered)
        raise DependencyCycleError(remaining)
    return ordered


def matching_node_ids(plan: ExecutionPlan, wanted: str) -> set[str]:
    return {
        node.id
        for node in plan.nodes
        if node.id == wanted or node.source_id == wanted
    }


def descendants_of(plan: ExecutionPlan, wanted: str) -> set[str] | None:
    seeds = matching_node_ids(plan, wanted)
    if not seeds:
        return None
    reverse: dict[str, list[str]] = {}
    for node in plan.nodes:
        for dependency in node.needs:
            reverse.setdefault(dependency, []).append(node.id)
    selected = set(seeds)
    stack = list(seeds)
    while stack:
        item = stack.pop()
        for child in reverse.get(item, []):
            if child not in selected:
                selected.add(child)
                stack.append(child)
    return selected


def ancestors_of(plan: ExecutionPlan, wanted: str) -> set[str] | None:
    seeds = matching_node_ids(plan, wanted)
    if not seeds:
        return None
    selected = set(seeds)
    stack = list(seeds)
    while stack:
        item = stack.pop()
        node = plan.node(item)
        if node is None:
            continue
        for dependency in node.needs:
            if dependency not in selected:
                selected.add(dependency)
                stack.append(dependency)
    return selected


def dependency_closure(plan: ExecutionPlan, selected: set[str]) -> set[str]:
    result = set(selected)
    stack = list(selected)
    while stack:
        item = stack.pop()
        node = plan.node(item)
        if node is None:
            continue
        for dependency in node.needs:
            if dependency not in result:
                result.add(dependency)
                stack.append(dependency)
    return result


def drop_step(workflow: WorkflowDefinition, step_id: str) -> WorkflowDefinition:
    """Elimina un paso fuente y reconecta a sus dependientes."""
    victim = next((step for step in workflow.steps if step.id == step_id), None)
    if victim is None:
        return workflow
    inherited = list(victim.needs)
    steps: list[StepDefinition] = []
    for step in workflow.steps:
        if step.id == step_id:
            continue
        if step_id in step.needs:
            needs = [item for item in step.needs if item != step_id]
            for parent in inherited:
                if parent not in needs:
                    needs.append(parent)
            step = replace(step, needs=needs)
        steps.append(step)
    return replace(workflow, steps=steps)
