"""Selección estructural de nodos según PipeCraft 1.3.1."""

from __future__ import annotations

from dataclasses import dataclass

from styler.runtime.graph import ancestors_of, dependency_closure, descendants_of, matching_node_ids, topological_order
from styler.runtime.models import ExecutionContext, ExecutionPlan


class SelectionError(ValueError):
    def __init__(self, message: str, code: str, **data: object):
        self.code = code
        self.data = data
        super().__init__(message)


@dataclass
class SelectionPreview:
    order: list[str]
    selected: list[str]
    excluded: list[str]
    reasons: dict[str, str]


def select_plan_nodes(plan: ExecutionPlan, ctx: ExecutionContext) -> set[str]:
    order = topological_order(plan)
    selected = {node.id for node in plan.nodes}

    if ctx.from_step:
        branch = descendants_of(plan, ctx.from_step)
        if branch is None:
            raise SelectionError(
                f"El nodo indicado en --from-step no existe: {ctx.from_step}",
                "FROM_NODE_NOT_FOUND",
                node=ctx.from_step,
            )
        selected &= branch

    if ctx.downstream_of:
        branch = descendants_of(plan, ctx.downstream_of)
        if branch is None:
            raise SelectionError(
                f"El nodo indicado en --downstream-of no existe: {ctx.downstream_of}",
                "DOWNSTREAM_NODE_NOT_FOUND",
                node=ctx.downstream_of,
            )
        selected &= branch

    if ctx.upstream_of:
        branch = ancestors_of(plan, ctx.upstream_of)
        if branch is None:
            raise SelectionError(
                f"El nodo indicado en --upstream-of no existe: {ctx.upstream_of}",
                "UPSTREAM_NODE_NOT_FOUND",
                node=ctx.upstream_of,
            )
        selected &= branch

    if ctx.only_steps:
        requested: set[str] = set()
        missing: list[str] = []
        for wanted in ctx.only_steps:
            matches = matching_node_ids(plan, wanted)
            if not matches:
                missing.append(wanted)
            requested |= matches
        if missing:
            raise SelectionError(
                f"--only hace referencia a nodos inexistentes: {', '.join(missing)}",
                "ONLY_NODE_NOT_FOUND",
                missing=missing,
            )
        selected &= requested

    if ctx.phases:
        selected = {node.id for node in plan.nodes if node.id in selected and node.phase in ctx.phases}
    if ctx.blocks:
        selected = {node.id for node in plan.nodes if node.id in selected and node.block in ctx.blocks}
    if ctx.skip_blocks:
        selected = {node.id for node in plan.nodes if node.id in selected and node.block not in ctx.skip_blocks}
    if ctx.include_needs:
        selected = dependency_closure(plan, selected)

    if not selected and order:
        raise SelectionError("Los filtros produjeron un plan vacío.", "EMPTY_SELECTION")
    return selected


def preview_selection(plan: ExecutionPlan, ctx: ExecutionContext) -> SelectionPreview:
    order = topological_order(plan)
    selected_set = select_plan_nodes(plan, ctx)
    reasons: dict[str, str] = {}
    for node_id in order:
        if node_id in selected_set:
            reasons[node_id] = "selected"
        else:
            reasons[node_id] = "selection_excluded"
    return SelectionPreview(
        order=order,
        selected=[node_id for node_id in order if node_id in selected_set],
        excluded=[node_id for node_id in order if node_id not in selected_set],
        reasons=reasons,
    )
