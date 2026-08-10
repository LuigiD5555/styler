"""Integración del catálogo con la restauración real (``restore.py``).

Reparto de responsabilidades, explícito para no duplicar trabajo:

``restore.py`` ya es el orquestador único y ya sabe hacer lo que sabe hacer:
escritorio → gestores → remotos → repos → aplicaciones → verificar → punto
de recuperación → archivos por etapas. **No se reescribe.**

Lo que ``restore.py`` NO sabe hacer, y es justamente lo que el catálogo
aporta, son los **overlays**: una pieza que no es una aplicación ni un
archivo suelto, sino una capa que se escribe ENCIMA de otra aplicación y
que sólo puede aplicarse después de que esa aplicación esté instalada *y
verificada* (PhotoGIMP sobre GIMP). El catálogo sabe:

- que PhotoGIMP requiere ``app.gimp.verified``;
- que su destino depende del proveedor con que GIMP quedó instalado;
- que hay que respaldar antes de escribir;
- y que si falla, no debe bloquear a VLC.

Este módulo produce ese trozo del plan —y sólo ese— como un
``WorkflowDefinition`` que ejecuta el scheduler existente. Los pasos de
instalación de las aplicaciones base no se duplican: se marcan como
satisfechos porque ``restore.py`` ya los hizo.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from styler.component_catalog.compiler import compile_workflow
from styler.component_catalog.executors import extended_registry
from styler.component_catalog.loader import load
from styler.component_catalog.models import ComponentDefinition
from styler.component_catalog.registry import ComponentRegistry
from styler.component_catalog.resolver import resolve
from styler.runtime.engine import WorkflowEngine
from styler.runtime.models import ExecutionContext, StepDefinition, StepResult, WorkflowDefinition

OVERLAY_KINDS = ("application_overlay", "configuration")


@dataclass
class OverlayPlan:
    """Los overlays aplicables a una restauración concreta."""

    workflow: WorkflowDefinition
    components: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.workflow.steps


def _installed_capabilities(source, registry: ComponentRegistry) -> set[str]:
    """Capacidades que la restauración base ya habrá satisfecho.

    Una aplicación de la configuración (``AppSpec``) se corresponde con un
    componente del catálogo si comparten el nombre de paquete o el
    application_id de algún proveedor. El entorno (``kde-plasma``) se
    corresponde por ``capability_alias``.
    """
    satisfied: set[str] = set()

    def matches(component: ComponentDefinition, manager: str, name: str) -> bool:
        lowered = name.lower()
        for provider in component.providers:
            if lowered in {pkg.lower() for pkg in provider.packages}:
                return True
            if provider.application_id and provider.application_id.lower() == lowered:
                return True
        return False

    for component in registry.all():
        for app in getattr(source, "applications", []):
            if matches(component, app.manager, app.name) or (
                app.identity and matches(component, app.manager, app.identity)
            ):
                satisfied.update(component.provides)
        environment_id = getattr(source, "environment_id", "")
        if environment_id and component.capability_alias == f"desktop.{environment_id}":
            satisfied.update(component.provides)

    return satisfied


def overlay_plan(source, family: str, root: str = ".") -> OverlayPlan:
    """Construye el plan de overlays que corresponde a esta restauración.

    Sólo se incluye un overlay si TODOS sus requisitos ya quedan
    satisfechos por lo que la restauración base va a instalar. Un overlay
    cuya dependencia no está en la configuración se omite con una
    advertencia legible, en vez de intentar instalar la dependencia por su
    cuenta y duplicar el trabajo de ``restore.py``.
    """
    registry = ComponentRegistry.from_report(load(root=root))
    satisfied = _installed_capabilities(source, registry)

    applicable: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []

    for component in registry.all():
        if component.kind not in OVERLAY_KINDS:
            continue
        missing = [req for req in component.requires if req not in satisfied]
        if missing:
            skipped.append(component.id)
            warnings.append(
                f"'{component.name}' se omite: la configuración no incluye lo que necesita ({', '.join(missing)})."
            )
            continue
        applicable.append(component.id)

    if not applicable:
        return OverlayPlan(
            workflow=WorkflowDefinition(name="overlays", steps=[]),
            skipped=tuple(skipped),
            warnings=warnings,
        )

    # El resolver arrastra las dependencias (GIMP) para poder calcular el
    # config_root correcto del overlay; luego se podan sus pasos, porque
    # restore.py ya las instaló.
    resolution = resolve(registry, applicable, family=family)
    compiled = compile_workflow(registry, resolution, name="overlays")
    warnings.extend(issue.message for issue in compiled.issues if issue.severity == "warning")

    overlay_ids = {
        component_id for component_id in resolution.selected_components
        if (component := registry.get(component_id)) and component.kind in OVERLAY_KINDS
    }
    kept: list[StepDefinition] = []
    for step in compiled.workflow.steps:
        owner = step.id.rsplit(".", 1)[0]
        if owner in overlay_ids:
            # Las aristas hacia pasos podados (GIMP) se eliminan: esa garantía
            # ya la dio restore.py al instalar y verificar la aplicación base.
            step.needs = [need for need in step.needs if need.rsplit(".", 1)[0] in overlay_ids]
            step.requires = []
            kept.append(step)

    return OverlayPlan(
        workflow=replace(compiled.workflow, steps=kept),
        components=tuple(sorted(overlay_ids)),
        skipped=tuple(skipped),
        warnings=warnings,
    )


def run_overlays(
    plan: OverlayPlan,
    root: str = ".",
    *,
    dry_run: bool = True,
) -> list[StepResult]:
    """Ejecuta overlays a través del plan compilado canónico."""
    if plan.empty:
        return []

    context = ExecutionContext(
        root=Path(root),
        dry_run=dry_run,
        run_id="overlays",
        labels=["styler", "overlay"],
    )
    run = WorkflowEngine(extended_registry()).run(plan.workflow, context)
    return run.results
