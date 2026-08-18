"""Validación estática de pipelines antes de compilar."""

from __future__ import annotations

from styler.planning.dependency_analysis import analyze_dependencies
from styler.planning.graph import DependencyCycleError, topological_order
from styler.planning.models import (
    DependencyMode,
    NodeKind,
    RunCondition,
    WorkflowDefinition,
    WorkflowOperation,
)

VALID_RISKS = {"low", "medium", "high"}
VALID_POLICIES = {"stop", "continue", "warn"}


def validate_workflow(workflow: WorkflowDefinition, known_types: set[str]) -> list[str]:
    errors: list[str] = []
    if workflow.schema_version != "pipecraft/v1":
        errors.append(f"Versión de esquema no soportada: '{workflow.schema_version}'.")
    if not workflow.name.strip():
        errors.append("El pipeline necesita un nombre.")
    if not workflow.steps:
        errors.append("El pipeline debe contener al menos un paso.")
    if workflow.operation not in WorkflowOperation.ALL:
        errors.append(f"Operación de pipeline inválida: '{workflow.operation}'.")
    if workflow.dependency_mode not in DependencyMode.ALL:
        errors.append(f"Modo de dependencias inválido: '{workflow.dependency_mode}'.")

    ids = [step.id for step in workflow.steps]
    known_ids = set(ids)
    seen: set[str] = set()

    for step in workflow.steps:
        if not step.id.strip():
            errors.append("Existe un paso sin identificador.")
        if step.id in seen:
            errors.append(f"Identificador de paso duplicado: {step.id}")
        seen.add(step.id)
        if step.step_type not in known_types:
            errors.append(f"Tipo de paso desconocido '{step.step_type}' en '{step.id}'.")
        if step.kind not in NodeKind.ALL:
            errors.append(f"Tipo de nodo inválido '{step.kind}' en '{step.id}'.")
        if step.run_if not in RunCondition.ALL:
            errors.append(f"Condición run_if inválida '{step.run_if}' en '{step.id}'.")
        if step.risk not in VALID_RISKS:
            errors.append(f"Riesgo inválido '{step.risk}' en '{step.id}'.")
        if step.retries < 0:
            errors.append(f"retries no puede ser negativo en '{step.id}'.")
        if step.retry_delay < 0:
            errors.append(f"retry_delay no puede ser negativo en '{step.id}'.")
        if step.timeout is not None and step.timeout <= 0:
            errors.append(f"timeout debe ser mayor que cero en '{step.id}'.")
        for dependency in step.needs:
            if dependency not in known_ids:
                errors.append(f"'{step.id}' depende de un paso inexistente: '{dependency}'.")
            if dependency == step.id:
                errors.append(f"'{step.id}' depende de sí mismo.")
        for reference in [*step.checks.before, *step.checks.after, *step.checks.on_failure]:
            if not reference.uses.strip():
                errors.append(f"'{step.id}' contiene un check sin identificador.")
            if reference.on_failure not in VALID_POLICIES:
                errors.append(f"Política de check inválida '{reference.on_failure}' en '{step.id}'.")

    for hook_name in workflow.hooks.__dataclass_fields__:
        for hook in getattr(workflow.hooks, hook_name):
            if not hook.id.strip():
                errors.append(f"Existe un hook sin id en '{hook_name}'.")
            if hook.step_type not in known_types:
                errors.append(f"Tipo de hook desconocido '{hook.step_type}' en '{hook.id}'.")
            if hook.run_if not in RunCondition.ALL:
                errors.append(f"Condición run_if inválida '{hook.run_if}' en hook '{hook.id}'.")

    policy_maps = [
        workflow.on_error.nodes,
        workflow.on_error.steps,
        workflow.on_error.blocks,
        workflow.on_error.phases,
        workflow.on_error.types,
        workflow.on_error.statuses,
    ]
    policies = [workflow.on_error.default]
    for mapping in policy_maps:
        policies.extend(mapping.values())
    for policy in policies:
        if policy not in VALID_POLICIES:
            errors.append(f"Política de error inválida: '{policy}'.")

    dependency_report = analyze_dependencies(workflow)
    errors.extend(issue.message for issue in dependency_report.issues)

    try:
        topological_order(workflow.steps)
    except DependencyCycleError as exc:
        errors.append(str(exc))
    return errors
