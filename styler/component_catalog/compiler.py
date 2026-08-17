"""Compilador hacia el DAG runtime (Fase 3).

Traduce componentes ya resueltos (``resolver.py``) a ``StepDefinition`` /
``WorkflowDefinition`` — los mismos modelos que ``styler.runtime.scheduler``
ya sabe ejecutar. No se crea un motor nuevo: el scheduler existente no
necesita saber qué es GIMP o KDE, solo ve pasos con ``needs`` (orden),
``requires``/``provides`` (capacidades) y recursos exclusivos/compartidos.

Cada componente genera hasta tres pasos:

- ``<id>.backup``   (solo si tiene rollback real y va a modificar algo existente)
- ``<id>.install``  (o "aplicar", si el componente no tiene proveedores propios)
- ``<id>.verify``

Las aristas entre componentes se resuelven por *capacidad*, no por
proveedor concreto (sección 20 del encargo): si un componente requiere
``app.gimp.verified``, su primer paso depende del paso ``app.gimp.verify``,
sin importar si GIMP se resolvió por APT o por Flatpak.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from styler.component_catalog.models import ComponentDefinition, ProviderDefinition
from styler.component_catalog.registry import ComponentRegistry
from styler.component_catalog.resolver import ResolutionResult
from styler.runtime.models import ErrorPolicy, NodeKind, PhaseDefinition, StepDefinition, WorkflowDefinition

STEP_TYPE_BY_KIND = {
    "application": "install_package",
    "desktop_environment": "install_package",
    "application_overlay": "install_overlay",
    "configuration": "apply_config",
    "service": "enable_service",
}


def _install_config(step_type: str, provider: ProviderDefinition | None) -> dict:
    """Config que cada ejecutor real necesita, según el tipo de paso.

    ``install_package`` reutiliza el ejecutor ya existente en
    ``styler.runtime.executors`` (espera ``config['package']``).
    ``install_overlay``/``apply_config`` usan los ejecutores nuevos de
    ``styler.component_catalog.executors``.
    """
    if provider is None:
        return {}
    if step_type == "install_package":
        name = provider.packages[0] if provider.packages else provider.application_id
        return {"package": {"manager": provider.type, "name": name}}
    if step_type == "install_overlay":
        return {"source": provider.source}
    return {}


def _target_root_for(
    component: ComponentDefinition,
    registry: ComponentRegistry,
    resolution: ResolutionResult,
) -> str:
    """Ruta real sobre la que este componente escribe (o "" si no hay).

    Un overlay (PhotoGIMP) no tiene ruta propia: escribe sobre la carpeta de
    configuración de la aplicación de la que depende, y esa ruta depende del
    proveedor con el que ESA aplicación se resolvió (APT → ~/.config/GIMP,
    Flatpak → ~/.var/app/...). Por eso se busca en el proveedor elegido del
    componente que satisface su 'requires'.

    Una 'configuration' (config KDE) sí declara su propia ruta en
    [resources.paths], porque no depende del gestor de paquetes.
    """
    # PhotoGIMP se distribuye como un overlay XDG: .config se aplica sobre
    # ~/.config y .local sobre ~/.local. Incluso cuando GIMP fue instalado
    # como Flatpak, el destino del overlay no debe heredarse del sandbox.
    if component.id == "app.photogimp":
        return "${HOME}/.config/GIMP"

    # 1. Ruta propia declarada por el componente (configurations).
    for resource in component.resources.exclusive:
        declared = component.resources.path_for(resource)
        if declared:
            return declared

    # 2. Ruta del proveedor elegido del propio componente (applications).
    #
    # Esta rama evita que app.gimp se
    # compilaba con target_root="" aun cuando el proveedor Flatpak sí declaraba
    # ``config_root``. El paso ``initialize_flatpak_app`` heredaba ese valor
    # vacío y fallaba después de instalar GIMP con:
    #
    #     Falta application_id o config_root.
    #
    # Un proveedor es la fuente de verdad de su ruta de configuración; no debe
    # ser necesario que la aplicación duplique esa ruta en [resources.paths].
    own_provider_id = resolution.selected_providers.get(component.id, "")
    own_config_root = component.config_root_for(own_provider_id)
    if own_config_root:
        return own_config_root

    # 3. Heredada del proveedor elegido de la dependencia (overlays).
    for decision in resolution.decisions:
        if decision.component_id != component.id or not decision.requirement:
            continue
        provider_component = registry.get(decision.chosen_component)
        if provider_component is None:
            continue
        provider_id = resolution.selected_providers.get(provider_component.id, "")
        config_root = provider_component.config_root_for(provider_id)
        if config_root:
            return config_root
    return ""


@dataclass(frozen=True)
class CompileIssue:
    severity: str  # error | warning
    code: str
    component_id: str
    message: str

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "component_id": self.component_id,
            "message": self.message,
        }


@dataclass
class CompileResult:
    workflow: WorkflowDefinition
    issues: list[CompileIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[CompileIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors


def _verify_step_id(component_id: str) -> str:
    return f"{component_id}.verify"


def _facts_step_id(component_id: str) -> str:
    return f"{component_id}.resolve-facts"


def _initialize_step_id(component_id: str) -> str:
    return f"{component_id}.initialize"


def _install_step_id(component_id: str) -> str:
    return f"{component_id}.install"


def _backup_step_id(component_id: str) -> str:
    return f"{component_id}.backup"


def _dependency_step(capability: str, providing_component_id: str) -> str:
    """A qué paso concreto del proveedor apunta un ``requires``.

    Una capacidad que termina en ``.verified`` (o cualquier otra que no sea
    explícitamente ``.installed``) exige la garantía más fuerte disponible:
    esperar al paso de verificación, no solo al de instalación.
    """
    if capability.endswith(".installed") and not capability.endswith(".verified"):
        return _install_step_id(providing_component_id)
    return _verify_step_id(providing_component_id)


_OVERLAY_KINDS = ("application_overlay", "configuration")


def _needs_backup(component: ComponentDefinition) -> bool:
    """Solo los componentes que modifican algo ya existente respaldan antes.

    Un ``application``/``desktop_environment`` normal instala paquetes: no
    hay estado de usuario que perder antes de instalar. Un
    ``application_overlay`` (PhotoGIMP) o una ``configuration`` (config KDE)
    sí escriben sobre configuración de usuario existente, así que necesitan
    el respaldo antes de tocar nada (sección 12 del encargo: "no debe copiar
    archivos silenciosamente antes de que KDE esté disponible").
    """
    return component.kind in _OVERLAY_KINDS and component.rollback.level in ("full", "best_effort")


def compile_workflow(
    registry: ComponentRegistry,
    resolution: ResolutionResult,
    *,
    name: str = "component-catalog-plan",
) -> CompileResult:
    issues: list[CompileIssue] = []
    steps: list[StepDefinition] = []

    components = [registry.get(cid) for cid in resolution.selected_components]
    components = [c for c in components if c is not None]

    for component in components:
        provider_id = resolution.selected_providers.get(component.id, "")
        has_provider = bool(provider_id) or not component.providers  # configs sin proveedor son válidas

        # Dependencias por capacidad: el primer paso del componente espera
        # al paso concreto (install o verify) del proveedor de cada requisito.
        dependency_steps = sorted(
            {
                _dependency_step(requirement, decision.chosen_component)
                for requirement in component.requires
                for decision in resolution.decisions
                if decision.component_id == component.id
                and decision.requirement == requirement
                and decision.chosen_component
            }
        )

        step_type = STEP_TYPE_BY_KIND.get(component.kind, "install")

        # 'criticality = optional' autoriza al resolutor a omitir el componente
        # del plan. No autoriza al planificador a convertir el fallo de un paso
        # ya incluido en una advertencia y declarar el cambio aplicado. Un
        # componente pedido explícitamente (la intención del usuario) siempre
        # produce pasos obligatorios.
        component_required = (
            component.criticality == "required"
            or component.id in resolution.requested_components
        )

        backup_needed = _needs_backup(component)
        target_root = _target_root_for(component, registry, resolution)

        if backup_needed:
            backup_step = StepDefinition(
                id=_backup_step_id(component.id),
                step_type="backup_config",
                description=f"Respaldo antes de aplicar {component.name}",
                needs=dependency_steps,
                requires=list(component.requires),
                provides=[],
                exclusive_resources=list(component.resources.exclusive),
                shared_resources=list(component.resources.shared),
                phase="backup",
                block=component.id,
                tags=[component.kind, "changes-system"],
                required=component_required,
                provider=provider_id,
                rollback=component.rollback.to_dict(),
                config=(
                    {
                        "backup_source": target_root,
                        **(
                            {
                                "runtime_facts_application_id": "org.gimp.GIMP",
                                "require_initialized_cycle": True,
                                "initialization_path_independent": True,
                            }
                            if component.id == "app.photogimp"
                            else {}
                        ),
                    }
                    if target_root
                    else {}
                ),
            )
            steps.append(backup_step)
            install_needs = [backup_step.id]
        else:
            install_needs = dependency_steps

        provider_def = next((p for p in component.providers if p.id == provider_id), None)
        install_config = _install_config(step_type, provider_def)
        if step_type == "install_overlay":
            install_config["semantic_operations"] = [
                {"operation": "handoff.download", "label": "Descargar el overlay autorizado"},
                {"operation": "overlay.apply", "label": "Copiar y respaldar archivos exactos"},
            ]
        if target_root:
            install_config["target"] = target_root
        if component.id == "app.photogimp":
            install_config.update({
                "runtime_facts_application_id": "org.gimp.GIMP",
                "require_initialized_cycle": True,
                "initialization_path_independent": True,
                "required_backup_step_id": _backup_step_id(component.id),
                "overlay_version_policy": "forward-template",
                "verify_overlay_manifest": True,
            })
        install_step = StepDefinition(
            id=_install_step_id(component.id),
            step_type=step_type,
            description=f"Instalar/aplicar {component.name}",
            needs=install_needs,
            requires=list(component.requires) if not backup_needed else [],
            provides=[cap for cap in component.provides if cap.endswith(".installed")] or list(component.provides),
            exclusive_resources=list(component.resources.exclusive),
            shared_resources=list(component.resources.shared),
            phase="install",
            block=component.id,
            tags=[component.kind, "changes-system"],
            required=component_required,
            provider=provider_id,
            rollback=component.rollback.to_dict(),
            config=install_config,
        )
        steps.append(install_step)

        verify_needs = [install_step.id]

        # Cuando PhotoGIMP forma parte del plan, GIMP Flatpak debe abrirse una
        # vez y cerrarse antes del respaldo. Esa primera ejecución crea la
        # carpeta de configuración de la versión real, resuelta desde Flatpak.
        if (
            component.id == "app.gimp"
            and provider_id == "flatpak"
            and "app.photogimp" in resolution.selected_components
        ):
            application_id = provider_def.application_id if provider_def is not None else ""
            initialization_root = provider_def.config_root if provider_def is not None else target_root
            if not application_id or not initialization_root:
                missing = []
                if not application_id:
                    missing.append("application_id")
                if not initialization_root:
                    missing.append("config_root")
                issues.append(
                    CompileIssue(
                        severity="error",
                        code="INVALID_FLATPAK_INITIALIZATION_CONFIG",
                        component_id=component.id,
                        message=(
                            "No se puede inicializar GIMP Flatpak: el proveedor "
                            f"'{provider_id}' no declara {', '.join(missing)}."
                        ),
                    )
                )
            facts_step = StepDefinition(
                id=_facts_step_id(component.id),
                step_type="resolve_flatpak_app_facts",
                description="Detectar la versión real de GIMP Flatpak",
                needs=[install_step.id],
                requires=[],
                provides=[],
                exclusive_resources=[],
                shared_resources=["flatpak:inventory"],
                phase="initialize",
                block=component.id,
                tags=[component.kind, "inspection", "runtime-facts"],
                required=True,
                provider=provider_id,
                config={
                    "application_id": application_id,
                    "config_root": initialization_root,
                },
            )
            steps.append(facts_step)
            initialize_step = StepDefinition(
                id=_initialize_step_id(component.id),
                step_type="initialize_flatpak_app",
                description="Abrir GIMP una vez para crear la configuración de su versión",
                needs=[facts_step.id],
                requires=[],
                provides=[],
                exclusive_resources=["user-config:gimp"],
                shared_resources=[],
                phase="initialize",
                block=component.id,
                tags=[component.kind, "changes-user-config"],
                required=True,
                provider=provider_id,
                config={
                    "application_id": application_id,
                    "startup_timeout_seconds": 90,
                    "poll_interval_seconds": 0.5,
                    "window_stable_seconds": 1.5,
                    "shutdown_timeout_seconds": 20,
                    "config_creation_timeout_seconds": 30,
                    "config_creation_max_seconds": 600,
                    "config_flush_timeout_seconds": 20,
                    "config_flush_max_seconds": 600,
                    "config_quiet_seconds": 2,
                    "config_root": initialization_root,
                    "semantic_operations": [
                        {"operation": "application.launch", "label": "Abrir GIMP mediante Flatpak"},
                        {
                            "operation": "wait.observable",
                            "label": "Esperar una ventana estable usando la versión detectada",
                        },
                        {
                            "operation": "application.stop",
                            "label": "Solicitar a GIMP que cierre y guarde su sesión",
                        },
                        {
                            "operation": "wait.observable",
                            "label": "Esperar el cierre y que la configuración deje de cambiar",
                        },
                    ],
                },
            )
            steps.append(initialize_step)
            verify_needs = [initialize_step.id]

        verify_checks = list(component.verification.checks)
        if component.id == "app.gimp" and provider_id == "flatpak":
            verify_checks = ["flatpak:org.gimp.GIMP"]
        if component.id == "app.photogimp":
            verify_checks = [
                "directory:user-config:gimp",
                "marker:photogimp",
                "photogimp:overlay",
            ]

        verify_step = StepDefinition(
            id=_verify_step_id(component.id),
            step_type="verify",
            description=f"Verificar {component.name}",
            needs=verify_needs,
            requires=[],
            provides=[cap for cap in component.provides if cap.endswith(".verified")] or list(component.provides),
            exclusive_resources=[],
            shared_resources=[],
            phase="verify",
            block=component.id,
            tags=[component.kind, "verification"],
            kind=NodeKind.CHECK,
            required=component_required,
            provider=provider_id,
            config={"checks": verify_checks, "target": target_root},
        )
        steps.append(verify_step)

        if not has_provider:
            issues.append(
                CompileIssue(
                    severity="error" if component.criticality == "required" else "warning",
                    code="NO_PROVIDER_SELECTED",
                    component_id=component.id,
                    message=f"'{component.id}' no tiene proveedor seleccionado por el resolver.",
                )
            )
        if not component.verification.verifiable and component.providers:
            issues.append(
                CompileIssue(
                    severity="warning",
                    code="NO_VERIFICATION_CHECKS",
                    component_id=component.id,
                    message=f"'{component.id}' no declara comprobaciones; el paso de verificación no puede afirmar nada.",
                )
            )

    cycle = _detect_step_cycle(steps)
    if cycle:
        issues.append(
            CompileIssue(
                severity="error",
                code="STEP_CYCLE",
                component_id=cycle[0],
                message=f"ciclo entre pasos: {' -> '.join(cycle)}",
            )
        )

    required_nodes = {
        step.id: "stop"
        for step in steps
        if step.required
    }
    optional_nodes = {
        step.id: "warn"
        for step in steps
        if not step.required
    }
    workflow = WorkflowDefinition(
        name=name,
        steps=steps,
        operation="apply",
        phases={
            "backup": PhaseDefinition("Respaldar estado modificable"),
            "install": PhaseDefinition("Instalar o aplicar componentes"),
            "initialize": PhaseDefinition("Inicializar aplicaciones que crean configuración"),
            "verify": PhaseDefinition("Comprobar el resultado", tags=["verification"]),
        },
        on_error=ErrorPolicy(default="stop", nodes={**required_nodes, **optional_nodes}),
        metadata={"max_workers": 4, "compiler": "styler-component-catalog/0.5"},
    )
    return CompileResult(workflow=workflow, issues=issues)


def _detect_step_cycle(steps: list[StepDefinition]) -> list[str]:
    graph = {step.id: list(step.needs) for step in steps}
    visited: set[str] = set()
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[str] = []

    def visit(node: str) -> bool:
        visited.add(node)
        stack.append(node)
        on_stack.add(node)
        for neighbor in graph.get(node, ()):
            if neighbor not in graph:
                continue
            if neighbor not in visited:
                if visit(neighbor):
                    return True
            elif neighbor in on_stack:
                start = stack.index(neighbor)
                found.extend(stack[start:] + [neighbor])
                return True
        stack.pop()
        on_stack.discard(node)
        return False

    for node in sorted(graph):
        if node not in visited:
            if visit(node):
                break
    return found
