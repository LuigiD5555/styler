"""Análisis estructural y de impacto para workflows declarativos.

Este módulo no ejecuta nada. Comprueba que las capacidades requeridas tienen
proveedor, que el proveedor está conectado mediante una dependencia del DAG y
calcula qué pasos quedarían afectados si se retira un paso o una capacidad.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from styler.runtime.models import StepDefinition, WorkflowDefinition


@dataclass(frozen=True)
class DependencyIssue:
    code: str
    step_id: str
    message: str
    related: tuple[str, ...] = ()


@dataclass
class DependencyReport:
    issues: list[DependencyIssue] = field(default_factory=list)
    providers: dict[str, list[str]] = field(default_factory=dict)
    consumers: dict[str, list[str]] = field(default_factory=dict)
    dependents: dict[str, list[str]] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not self.issues


def analyze_dependencies(workflow: WorkflowDefinition) -> DependencyReport:
    """Valida capacidades y aristas causales del DAG.

    Una capacidad requerida no basta con existir en cualquier parte del plan:
    el consumidor debe depender, directa o transitivamente, de al menos uno de
    sus proveedores. Esto evita que una personalización se ejecute antes que la
    aplicación que necesita.
    """
    steps = workflow.steps
    by_id = {step.id: step for step in steps}
    providers: dict[str, list[str]] = {}
    consumers: dict[str, list[str]] = {}
    dependents: dict[str, list[str]] = {step.id: [] for step in steps}

    for step in steps:
        for capability in step.provides:
            providers.setdefault(capability, []).append(step.id)
        for capability in step.requires:
            consumers.setdefault(capability, []).append(step.id)
        for dependency in step.needs:
            if dependency in dependents:
                dependents[dependency].append(step.id)

    issues: list[DependencyIssue] = []
    for step in steps:
        ancestors = _ancestors(step.id, by_id)
        for capability in step.requires:
            candidates = providers.get(capability, [])
            if not candidates:
                issues.append(DependencyIssue(
                    "MISSING_CAPABILITY_PROVIDER",
                    step.id,
                    f"'{step.id}' requiere '{capability}', pero ningún paso la proporciona.",
                    (capability,),
                ))
                continue
            connected = [provider for provider in candidates if provider == step.id or provider in ancestors]
            if not connected:
                issues.append(DependencyIssue(
                    "UNORDERED_CAPABILITY_PROVIDER",
                    step.id,
                    f"'{step.id}' requiere '{capability}', pero no depende de su proveedor. "
                    "Podría ejecutarse antes de que la aplicación requerida esté lista.",
                    tuple(candidates),
                ))

    return DependencyReport(issues, providers, consumers, dependents)


def impacted_steps(workflow: WorkflowDefinition, removed_step_ids: set[str]) -> list[str]:
    """Devuelve todos los pasos que quedarían rotos al retirar pasos dados.

    Incluye dependientes por aristas ``needs`` y consumidores cuyas capacidades
    dejarían de tener un proveedor disponible.
    """
    report = analyze_dependencies(workflow)
    by_id = {step.id: step for step in workflow.steps}
    impacted = set(removed_step_ids)
    changed = True
    while changed:
        changed = False
        for step in workflow.steps:
            if step.id in impacted:
                continue
            if any(dependency in impacted for dependency in step.needs):
                impacted.add(step.id)
                changed = True
                continue
            for capability in step.requires:
                available = [provider for provider in report.providers.get(capability, []) if provider not in impacted]
                if not available:
                    impacted.add(step.id)
                    changed = True
                    break
    return [step.id for step in workflow.steps if step.id in impacted]


def impacted_by_capability(workflow: WorkflowDefinition, capability: str) -> list[str]:
    """Calcula el impacto de retirar todos los proveedores de una capacidad."""
    report = analyze_dependencies(workflow)
    return impacted_steps(workflow, set(report.providers.get(capability, [])))


def _ancestors(step_id: str, by_id: dict[str, StepDefinition]) -> set[str]:
    found: set[str] = set()
    pending = list(by_id.get(step_id, StepDefinition("", "")).needs)
    while pending:
        dependency = pending.pop()
        if dependency in found:
            continue
        found.add(dependency)
        parent = by_id.get(dependency)
        if parent is not None:
            pending.extend(parent.needs)
    return found
