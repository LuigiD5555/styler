"""El pipeline de PhotoGIMP debe ejecutarse de verdad y no fingir éxito.

Las tres reglas que estas pruebas congelan:

1. Un paso del cambio que la persona pidió no puede fallar en silencio, aunque
   el componente esté marcado como ``criticality = "optional"``.
2. Una ejecución nueva ejecuta todos sus pasos; la reconciliación solo puede
   saltarse trabajo cuando se está retomando un intento anterior.
3. La evidencia del ciclo controlado de apertura y cierre de GIMP sobrevive a
   las consultas de solo lectura, y se descarta cuando GIMP cambia de versión.
"""
from __future__ import annotations
from styler.execution.registry import default_registry

from pathlib import Path

from styler.changes import ChangeService
from styler.component_catalog.compiler import compile_workflow
from styler.component_catalog.loader import load
from styler.component_catalog.registry import ComponentRegistry
from styler.component_catalog.resolver import resolve
from styler.flatpak_facts import (
    FlatpakApplicationFacts,
    load_flatpak_facts,
    save_flatpak_facts,
)
from tests.support.local_engine import WorkflowEngine
from styler.execution.base import ExecutorRegistry, StepExecutor
from styler.planning.models import (
    ExecutionContext,
    Status,
    StepDefinition,
    StepResult,
    WorkflowDefinition,
)


def _photogimp_workflow(root: Path) -> WorkflowDefinition:
    registry = ComponentRegistry.from_report(load(root=root))
    resolution = resolve(
        registry,
        ["app.photogimp"],
        family="ubuntu",
        preferred_providers={"app.gimp": "flatpak"},
    )
    compiled = compile_workflow(registry, resolution, name="integrate-photogimp")
    assert compiled.ok, [issue.message for issue in compiled.errors]
    return compiled.workflow


# --------------------------------------------------------------------- regla 1
def test_requested_component_steps_are_mandatory_despite_optional_criticality(tmp_path: Path):
    """PhotoGIMP declara 'optional' y aun así sus pasos deben ser obligatorios.

    'optional' autoriza al resolutor a omitir el componente del plan. No
    autoriza al planificador a convertir el fallo de la copia o de la
    verificación en una advertencia y declarar el cambio aplicado.
    """
    workflow = _photogimp_workflow(tmp_path)
    photogimp_steps = [step for step in workflow.steps if step.id.startswith("app.photogimp.")]

    assert photogimp_steps, "el plan debe contener los pasos de PhotoGIMP"
    for step in photogimp_steps:
        assert step.required is True, f"{step.id} no puede ser opcional"


def test_failed_overlay_is_reported_as_failure_not_as_warning(tmp_path: Path):
    """Un fallo real no puede terminar como '✓ Advertencia opcional'."""

    class AlwaysFails(StepExecutor):
        @property
        def step_type(self) -> str:
            return "install_overlay"

        def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
            return StepResult.failed(step, "No se pudo descargar el overlay.", "OVERLAY_DOWNLOAD_FAILED")

    step = StepDefinition(
        "app.photogimp.install",
        "install_overlay",
        required=True,
        config={},
    )
    registry = default_registry()
    registry.register(AlwaysFails())
    run = WorkflowEngine(registry).run(
        WorkflowDefinition("overlay", [step]),
        ExecutionContext(root=tmp_path, dry_run=False, approve=True),
    )

    assert not run.success
    assert run.results[0].status == Status.FAILED
    assert "Advertencia" not in run.results[0].message


# --------------------------------------------------------------------- regla 2
def test_fresh_run_executes_every_step_without_reconciling(tmp_path: Path):
    """Sin modo continuación, ningún paso puede darse por hecho."""
    executed: list[str] = []

    class Recording(StepExecutor):
        @property
        def step_type(self) -> str:
            return "install_package"

        def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
            return StepResult(step.id, step.step_type, True, Status.RECONCILED, "Ya estaba hecho.")

        def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
            executed.append(step.id)
            return StepResult(step.id, step.step_type, True, Status.OK, "Ejecutado de verdad.")

    step = StepDefinition("app.gimp.install", "install_package", required=True)
    registry = default_registry()
    registry.register(Recording())

    run = WorkflowEngine(registry).run(
        WorkflowDefinition("fresh", [step]),
        ExecutionContext(root=tmp_path, dry_run=False, approve=True, values={}),
    )

    assert executed == ["app.gimp.install"]
    assert run.results[0].status != Status.RECONCILED


def test_continuation_run_may_still_reuse_completed_steps(tmp_path: Path):
    """Retomar un intento fallido sí puede reutilizar lo ya completado."""
    executed: list[str] = []

    class Recording(StepExecutor):
        @property
        def step_type(self) -> str:
            return "install_package"

        def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
            return StepResult(step.id, step.step_type, True, Status.RECONCILED, "Ya estaba hecho.")

        def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
            executed.append(step.id)
            return StepResult(step.id, step.step_type, True, Status.OK, "Ejecutado.")

    step = StepDefinition("app.gimp.install", "install_package", required=True)
    registry = default_registry()
    registry.register(Recording())

    run = WorkflowEngine(registry).run(
        WorkflowDefinition("continuation", [step]),
        ExecutionContext(
            root=tmp_path,
            dry_run=False,
            approve=True,
            values={"continuation_mode": True},
        ),
    )

    assert executed == []
    assert run.results[0].status == Status.RECONCILED


def test_initialize_step_runs_on_a_fresh_plan(tmp_path: Path):
    """El paso de abrir GIMP existe y no queda marcado como reutilizable."""
    service = ChangeService(tmp_path / "library", tmp_path / "home")
    plan = service.build_plan("photogimp", "flatpak")

    step_ids = [phase.step_id for phase in plan.phases]
    assert "app.gimp.initialize" in step_ids
    assert plan.continuation_mode is False
    assert plan.reconciled_steps == {}


# --------------------------------------------------------------------- regla 3
def _facts(version: str) -> FlatpakApplicationFacts:
    return FlatpakApplicationFacts(
        application_id="org.gimp.GIMP",
        installed=True,
        version=version,
        branch="stable",
        ref=f"app/org.gimp.GIMP/x86_64/stable/{version}",
        config_schema=version.rpartition(".")[0] or version,
    )


def test_read_only_probe_keeps_initialization_evidence(tmp_path: Path):
    """Una consulta posterior no puede borrar la evidencia del ciclo."""
    save_flatpak_facts(
        tmp_path,
        _facts("3.0.4"),
        initialization_completed=True,
        initialized_config_path="/home/u/.var/app/org.gimp.GIMP/config/GIMP/3.0",
    )

    # Reconciliación de install_package: reescribe los hechos observados.
    save_flatpak_facts(tmp_path, _facts("3.0.4"))

    stored = load_flatpak_facts(tmp_path, "org.gimp.GIMP") or {}
    assert stored.get("initialization_completed") is True
    assert stored.get("initialized_config_path", "").endswith("/GIMP/3.0")


def test_evidence_is_dropped_when_gimp_changes_version(tmp_path: Path):
    """Tras una actualización, el ciclo debe volver a ejecutarse de verdad."""
    save_flatpak_facts(
        tmp_path,
        _facts("3.0.4"),
        initialization_completed=True,
        initialized_config_path="/home/u/.var/app/org.gimp.GIMP/config/GIMP/3.0",
    )

    save_flatpak_facts(tmp_path, _facts("4.0.1"))

    stored = load_flatpak_facts(tmp_path, "org.gimp.GIMP") or {}
    assert "initialization_completed" not in stored
    assert stored.get("version") == "4.0.1"
