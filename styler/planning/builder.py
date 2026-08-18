"""Convierte componentes revisados en un workflow interno, sin YAML."""

from __future__ import annotations

import re
from dataclasses import asdict

from styler.models import Changeset
from styler.planning.models import ErrorPolicy, PhaseDefinition, StepDefinition, WorkflowDefinition


def workflow_from_changeset(changeset: Changeset, name: str | None = None) -> WorkflowDefinition:
    included = changeset.included()
    steps_by_component: dict[str, list[StepDefinition]] = {}
    roots_by_component: dict[str, list[str]] = {}
    terminals_by_component: dict[str, list[str]] = {}

    for component in included:
        component_steps: list[StepDefinition] = []
        package_ids: list[str] = []

        for index, package in enumerate(component.packages):
            suffix = "install" if len(component.packages) == 1 else f"install-{index + 1}-{_slug(package.name)}"
            step_id = f"{component.component_id}__{suffix}"
            package_ids.append(step_id)
            component_steps.append(
                StepDefinition(
                    id=step_id,
                    step_type="install_package",
                    description=f"Instalar {package.name} mediante {package.manager}",
                    risk="medium",
                    requires_approval=True,
                    config={"component_id": component.component_id, "package": asdict(package)},
                    retries=1,
                    retry_delay=0.25,
                    timeout=300,
                    exclusive_resources=[package.manager, "dpkg"] if package.manager == "apt" else [package.manager],
                    shared_resources=["network"],
                    provider=package.manager,
                    phase="applications",
                    provides=[f"package:{package.manager}:{package.name}:installed"],
                )
            )

        overlay_id: str | None = None
        if component.files:
            overlay_id = f"{component.component_id}__overlay"
            component_steps.append(
                StepDefinition(
                    id=overlay_id,
                    step_type="apply_file_overlay",
                    description=f"Aplicar archivos de {component.title}",
                    needs=list(package_ids),
                    risk="medium",
                    requires_approval=True,
                    config={
                        "component_id": component.component_id,
                        "files": [file_entry.to_dict() for file_entry in component.files],
                    },
                    exclusive_resources=["user-config"],
                    phase="configuration",
                )
            )

        service_ids: list[str] = []
        for index, service in enumerate(component.services):
            suffix = _slug(service.name) or str(index + 1)
            step_id = f"{component.component_id}__service-{suffix}"
            service_ids.append(step_id)
            service_needs = list(package_ids)
            if overlay_id:
                service_needs.append(overlay_id)
            component_steps.append(
                StepDefinition(
                    id=step_id,
                    step_type="enable_service",
                    description=f"Habilitar servicio {service.name}",
                    needs=service_needs,
                    risk="high" if service.scope == "system" else "medium",
                    requires_approval=True,
                    config={"component_id": component.component_id, "service": asdict(service)},
                    timeout=60,
                    exclusive_resources=["session-manager"] if service.scope == "system" else ["user-services"],
                    phase="services",
                )
            )

        if not component_steps:
            component_steps.append(
                StepDefinition(
                    id=f"{component.component_id}__note",
                    step_type="note",
                    description=component.human_summary or component.title,
                    config={"component_id": component.component_id},
                    required=False,
                )
            )

        for step in component_steps:
            step.block = component.component_id
            step.tags = _dedupe([*step.tags, "changeset", component.component_id])

        internal_ids = {step.id for step in component_steps}
        roots = [step.id for step in component_steps if not set(step.needs) & internal_ids]
        if service_ids:
            terminals = service_ids
        elif overlay_id:
            terminals = [overlay_id]
        elif package_ids:
            terminals = package_ids
        else:
            terminals = [component_steps[-1].id]

        steps_by_component[component.component_id] = component_steps
        roots_by_component[component.component_id] = roots
        terminals_by_component[component.component_id] = terminals

    # Una dependencia entre componentes bloquea el inicio del componente actual
    # hasta que hayan terminado todos los pasos terminales del componente requerido.
    included_by_id = {component.component_id: component for component in included}
    for component_id, component in included_by_id.items():
        external_needs: list[str] = []
        for dependency in component.depends_on:
            external_needs.extend(terminals_by_component.get(dependency, []))
        if not external_needs:
            continue
        root_ids = set(roots_by_component[component_id])
        for step in steps_by_component[component_id]:
            if step.id in root_ids:
                step.needs = _dedupe(step.needs + external_needs)

    steps: list[StepDefinition] = []
    for component in included:
        steps.extend(steps_by_component[component.component_id])

    return WorkflowDefinition(
        name=name or f"styler-{changeset.changeset_id}",
        description="Aplicación de componentes revisados de Styler.",
        metadata={
            "changeset_id": changeset.changeset_id,
            "base_state": changeset.base_state,
            "target_state": changeset.target_state,
            "components": [component.component_id for component in included],
        },
        steps=steps,
        phases={
            "applications": PhaseDefinition("Instalar aplicaciones"),
            "configuration": PhaseDefinition("Aplicar configuración"),
            "services": PhaseDefinition("Habilitar servicios"),
        },
        on_error=ErrorPolicy(default="stop", statuses={"needs_approval": "stop"}),
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
