"""Planificación y frontera de ejecución de workflows de Styler.

Styler posee la semántica del workflow (modelos, validación, reconciliación y
compilación del plan). PipeCraft posee la ejecución productiva. Este módulo es
la única puerta que conecta ambas responsabilidades.

No contiene scheduler, gestión de procesos, reintentos ni journal de ejecución.
Esas responsabilidades pertenecen a PipeCraft.
"""
from __future__ import annotations

from typing import Protocol

from styler.execution.base import ExecutorRegistry
from styler.execution.registry import default_registry
from styler.planning.graph import topological_order
from styler.planning.models import (
    ExecutionContext,
    ExecutionPlan,
    Status,
    StepDefinition,
    StepResult,
    WorkflowDefinition,
    WorkflowRun,
)
from styler.planning.plan import compile_workflow
from styler.planning.selection import SelectionPreview, preview_selection
from styler.planning.validation import validate_workflow


class WorkflowExecutionBackend(Protocol):
    """Contrato mínimo de un backend de ejecución.

    La implementación productiva es PipeCraft. El protocolo existe para poder
    probar la semántica de Styler sin incrustar un segundo scheduler productivo.
    """

    def __call__(
        self,
        workflow: WorkflowDefinition,
        context: ExecutionContext,
        plan: ExecutionPlan,
        registry: ExecutorRegistry,
    ) -> WorkflowRun: ...


class WorkflowPlanner:
    """Valida, compila, previsualiza y reconcilia; nunca ejecuta efectos."""

    def __init__(self, registry: ExecutorRegistry | None = None) -> None:
        self.registry = registry or default_registry()

    def validate(self, workflow: WorkflowDefinition) -> list[str]:
        errors = validate_workflow(workflow, self.registry.known_types() | {"command"})
        from styler.methods import validate_method_bindings

        errors.extend(validate_method_bindings(workflow))
        return errors

    def compile(self, workflow: WorkflowDefinition) -> ExecutionPlan:
        errors = self.validate(workflow)
        if errors:
            raise ValueError("\n".join(errors))
        return compile_workflow(workflow)

    def order(self, workflow: WorkflowDefinition) -> list[str]:
        return topological_order(self.compile(workflow))

    def preview(
        self,
        workflow: WorkflowDefinition,
        context: ExecutionContext | None = None,
    ) -> SelectionPreview:
        return preview_selection(self.compile(workflow), context or ExecutionContext())

    def reconcile(
        self,
        workflow: WorkflowDefinition,
        context: ExecutionContext | None = None,
    ) -> dict[str, StepResult]:
        """Consulta efectos ya satisfechos sin ejecutar acciones."""
        ctx = context or ExecutionContext()
        by_id = {step.id: step for step in workflow.steps}
        results: dict[str, StepResult] = {}
        for step_id in topological_order(workflow.steps):
            step = by_id.get(step_id)
            if step is None:
                continue
            result = self._reconcile_step(step, ctx)
            if result is not None:
                results[step.id] = result
        return results

    def _reconcile_step(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        executor = self.registry.get(step.step_type)
        if executor is None:
            return None
        try:
            result = executor.reconcile(step, ctx)
        except Exception as exc:
            ctx.values.setdefault("reconciliation_warnings", []).append(
                {"step_id": step.id, "step_type": step.step_type, "error": str(exc)}
            )
            return None
        if result is None:
            return None
        if not result.success:
            ctx.values.setdefault("reconciliation_warnings", []).append(
                {"step_id": step.id, "step_type": step.step_type, "error": result.message}
            )
            return None
        result.data.setdefault("reconciled", True)
        result.status = Status.RECONCILED
        return result


def _pipecraft_execute(
    workflow: WorkflowDefinition,
    context: ExecutionContext,
    plan: ExecutionPlan,
    registry: ExecutorRegistry,
) -> WorkflowRun:
    # `registry` pertenece a Styler y se usa al construir/validar el plan; los
    # executors concretos se invocan después como plugins externos de PipeCraft.
    del registry
    from styler.pipecraft.engine import PipeCraftBackend
    from styler.pipecraft.service import ensure_service

    ensure_service(context.root)
    return PipeCraftBackend(context.root).run(workflow, context, plan)


# Una única referencia intercambiable permite a los tests usar un ejecutor
# determinista sin mantener un scheduler alternativo dentro del paquete Styler.
_execution_backend: WorkflowExecutionBackend = _pipecraft_execute


def execute(
    workflow: WorkflowDefinition,
    context: ExecutionContext,
    registry: ExecutorRegistry | None = None,
) -> WorkflowRun:
    """Ejecuta un workflow usando la única autoridad productiva: PipeCraft."""
    resolved_registry = registry or default_registry()
    plan = WorkflowPlanner(resolved_registry).compile(workflow)
    return _execution_backend(workflow, context, plan, resolved_registry)
