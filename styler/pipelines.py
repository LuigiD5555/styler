"""
styler.pipelines
================
Dos pipelines, no uno. Con una compuerta entre ellos.

    PIPELINE 1 — ENTORNO            PIPELINE 2 — PERSONALIZACIÓN
    ─────────────────────           ────────────────────────────
    autorizar (una vez)             punto de recuperación
    refrescar catálogos             archivos por etapas
    escritorio (KDE Plasma)         verificación de la escritura
    gestores y remotos              rollback si algo falla
    aplicaciones
    VERIFICAR                       ← COMPUERTA: solo corre si el
                                      entorno está instalado Y verificado

Por qué separados:

* **Naturaleza distinta.** El entorno toca el sistema con privilegios y NO se
  puede deshacer. La personalización toca tu HOME, es transaccional y sí se
  deshace. Mezclarlos en un solo pipeline obliga a mentir sobre el rollback.
* **Se repiten distinto.** El entorno es idempotente: puedes reintentarlo mil
  veces y lo ya instalado no se repite. Los archivos se aplican de golpe, con
  respaldo.
* **Se usan por separado.** Preparar una máquina nueva hoy (entorno) y traerte
  tu configuración mañana (personalización) es un caso real.

Y aun así: `run_all()` los encadena con una sola aprobación. La persona da
Aceptar una vez y no vuelve a intervenir — la autorización se mantiene viva sola
durante toda la instalación (`styler/privileges.py::SudoTicket`).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from styler import orchestrator
from styler import restore as restore_mod
from styler import target as target_mod
from styler import verification as verify_mod
from styler.applications import ProgressCallback
from styler.restore import ItemStatus, RestorePlan, RestoreReport
from styler.runtime.commands import Runner, PipeCraftRunner

ENVIRONMENT = "entorno"
PERSONALIZATION = "personalizacion"
ALL = "todo"

PIPELINES = (ENVIRONMENT, PERSONALIZATION, ALL)

TITLES = {
    ENVIRONMENT: "Entorno (escritorio, gestores y aplicaciones)",
    PERSONALIZATION: "Personalización (tus archivos)",
    ALL: "Restauración completa",
}


@dataclass
class GateResult:
    open: bool
    reason: str = ""
    verification: Optional[verify_mod.VerificationResult] = None


def gate(
    plan: RestorePlan,
    runner: Runner | None = None,
    root: str = ".",
) -> GateResult:
    """¿Está el entorno listo para que se copien archivos? Se comprueba, no se supone."""
    runner = runner or PipeCraftRunner()
    result = verify_mod.verify_requirements(
        environment_id=next(
            (item.key.split(":", 1)[1] for item in plan.items if item.kind == "desktop"), ""
        ),
        managers=[item.key.split(":", 1)[1] for item in plan.items if item.kind == "manager"],
        remotes=[item.key.split(":", 1)[1] for item in plan.items if item.kind == "remote"],
        candidates=[
            (item.key, item.title, item.candidate, item.mandatory)
            for item in plan.items
            if item.kind == "application" and item.status != ItemStatus.SKIPPED_BY_USER
        ],
        runner=runner,
        root=root,
    )
    if result.ok:
        return GateResult(True, verification=result)
    faltan = ", ".join(check.title for check in result.failures())
    return GateResult(
        False,
        (
            f"El entorno todavía no está completo: {faltan}. "
            "Ejecuta primero el pipeline de entorno («styler pipeline entorno»); "
            "Styler no copiará ningún archivo hasta que esté instalado y verificado."
        ),
        result,
    )


def run(
    pipeline: str,
    source_type: str,
    source_id: str,
    root: str = ".",
    home: str | Path | None = None,
    execute: bool = False,
    approve: bool = False,
    runner: Runner | None = None,
    target: target_mod.Target | None = None,
    privilege: str = "auto",
    refresh_index: bool = True,
    skip: Iterable[str] = (),
    install_apps: bool = True,
    is_root: bool | None = None,
    progress: ProgressCallback = None,
) -> RestoreReport:
    """Ejecuta uno de los pipelines, o los dos encadenados.

    Una sola aprobación cubre todo lo que la persona ya vio en el plan.
    """
    if pipeline not in PIPELINES:
        raise ValueError(f"Pipeline desconocido: {pipeline}")

    runner = runner or PipeCraftRunner()
    plan = orchestrator.plan_restore(
        source_type,
        source_id,
        root=root,
        runner=runner,
        target=target,
        privilege=privilege,
        skip=skip,
        install_apps=install_apps,
        is_root=is_root,
    )
    source = orchestrator._source(source_type, source_id, root)

    if pipeline == PERSONALIZATION:
        # No se instala nada: solo se comprueba y se escribe.
        report = RestoreReport(plan=plan, dry_run=not execute, started_at=_now())
        decision = gate(plan, runner, root)
        report.verification = decision.verification
        report.environment_ready = decision.open
        if not decision.open:
            report.aborted_reason = decision.reason
            report.finished_at = _now()
            return report
        if not execute:
            report.finished_at = _now()
            return report
        if not approve:
            report.aborted_reason = "Escribir archivos requiere tu aprobación explícita."
            report.finished_at = _now()
            return report
        return restore_mod._finish_environment_and_files(
            plan, report, runner, root, home, source.label, progress, files=True
        )

    return restore_mod.execute(
        plan,
        root=root,
        home=home,
        execute_real=execute,
        approve=approve,
        runner=runner,
        refresh_index=refresh_index,
        progress=progress,
        label=source.label,
        target=target,
        privilege=privilege,
        is_root=is_root,
        apply_files=(pipeline == ALL),
    )


def _now() -> float:
    import time

    return time.time()
