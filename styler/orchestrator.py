"""
styler.orchestrator
===================
Fachada del flujo de restauración. Un solo camino:

    leer perfil o snapshot
        → detectar la distribución destino
        → construir requisitos (escritorio, gestores, remotos, repositorios, apps)
        → instalar en ese orden
        → VERIFICAR
        → punto de recuperación
        → aplicar archivos por etapas
        → verificar y reportar

La lógica vive en `styler.restore`; aquí solo se adapta a lo que ya consumen la
CLI, los servicios y la TUI. Ningún archivo se escribe antes de que el entorno
esté instalado y verificado.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from styler import applications as apps_mod
from styler import restore as restore_mod
from styler import target as target_mod
from styler.applications import InstallPlan, ProgressCallback, Runner
from styler.restore import ItemStatus, RestorePlan, RestoreReport, RestoreSource

FAILED_STATUSES = {
    ItemStatus.FAILED,
    ItemStatus.VERIFICATION_FAILED,
    ItemStatus.PERMISSION_DENIED,
    ItemStatus.MANUAL_REQUIRED,
    ItemStatus.MANAGER_MISSING,
    ItemStatus.UNSUPPORTED,
}


@dataclass
class ApplyOutcome:
    """Resultado completo: requisitos, verificación y archivos."""

    source_type: str
    source_id: str
    dry_run: bool
    plan: RestorePlan
    install_plan: InstallPlan          # vista de solo-aplicaciones (compatibilidad)
    report: Optional[RestoreReport] = None
    warnings: list[str] = field(default_factory=list)

    @property
    def install_report(self):
        return self.report

    @property
    def run(self):
        return _Run(self)

    @property
    def record(self):
        return _Record(self)

    @property
    def aborted_reason(self) -> str:
        return self.report.aborted_reason if self.report else ""

    @property
    def files_applied(self) -> bool:
        return bool(self.report and self.report.files_applied)

    @property
    def ok(self) -> bool:
        return bool(self.report and self.report.ok)

    def _apps_with(self, statuses: set[str]) -> list[str]:
        return [
            item.key.split(":", 1)[1]
            for item in self.plan.items
            if item.kind == "application" and item.status in statuses
        ]

    @property
    def installed_apps(self) -> list[str]:
        return self._apps_with({ItemStatus.INSTALLED})

    @property
    def failed_apps(self) -> list[str]:
        return self._apps_with(FAILED_STATUSES)

    @property
    def needs_relogin(self) -> bool:
        return bool(self.report and self.report.needs_relogin)

    def human_message(self) -> str:
        return self.report.human_message() if self.report else "Sin ejecutar."

    def to_dict(self) -> dict[str, Any]:
        return self.report.to_dict() if self.report else self.plan.to_dict()


@dataclass
class _StepView:
    step_id: str
    status: str
    data: dict


@dataclass
class _Run:
    """Compatibilidad con quien esperaba un WorkflowRun de archivos."""

    outcome: "ApplyOutcome"

    @property
    def success(self) -> bool:
        return self.outcome.ok

    @property
    def report_path(self) -> str:
        return ""

    @property
    def results(self) -> list:
        if self.outcome.report is None:
            return []
        status = "dry_run" if self.outcome.dry_run else "ok"
        return [
            _StepView(step_id=entry.path, status=status, data={"path": entry.path})
            for stage in self.outcome.plan.file_stages
            for entry in stage.entries
        ]


@dataclass
class _Record:
    outcome: "ApplyOutcome"

    @property
    def applied(self) -> bool:
        return self.outcome.files_applied

    @property
    def transaction_id(self) -> str:
        return self.outcome.report.transaction_id if self.outcome.report else ""

    @property
    def backup_snapshot(self) -> str:
        return self.outcome.report.recovery_point if self.outcome.report else ""

    @property
    def rollback_status(self) -> str:
        return self.outcome.report.rollback_status if self.outcome.report else ""

    @property
    def rolled_back(self) -> bool:
        return self.rollback_status == "completed"

    @property
    def error(self) -> str:
        return self.outcome.report.aborted_reason if self.outcome.report else ""

    @property
    def warnings(self) -> list[str]:
        return list(self.outcome.report.warnings) if self.outcome.report else []


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

def _source(source_type: str, source_id: str, root: str) -> RestoreSource:
    if source_type == "snapshot":
        return restore_mod.source_from_snapshot(source_id, root=root)
    return restore_mod.source_from_profile(source_id, root=root)


def plan_restore(
    source_type: str,
    source_id: str,
    root: str = ".",
    runner: Runner | None = None,
    target: target_mod.Target | None = None,
    privilege: str = "auto",
    skip: Iterable[str] = (),
    install_apps: bool = True,
    is_root: bool | None = None,
) -> RestorePlan:
    """El plan completo y visible que la persona aprueba una sola vez."""
    source = _source(source_type, source_id, root)
    skipped = set(skip)
    if not install_apps:
        skipped.update(app.app_id for app in source.applications)
    plan = restore_mod.build_plan(
        source,
        root=root,
        runner=runner,
        target=target,
        privilege=privilege,
        skip=skipped,
        install_desktop=install_apps,
        is_root=is_root,
    )
    if not install_apps and source.applications:
        plan.warnings.append(
            f"Se pidió no instalarlas: {len(source.applications)} aplicaciones quedan "
            "fuera. La configuración puede quedar sin efecto donde la app no exista."
        )
    return plan


def _apply(
    source_type: str,
    source_id: str,
    root: str,
    execute: bool,
    approve: bool,
    home: str | Path | None,
    install_apps: bool,
    runner: Runner | None,
    privilege: str,
    refresh_index: bool,
    skip: Iterable[str],
    progress: ProgressCallback,
    target: target_mod.Target | None = None,
    is_root: bool | None = None,
) -> ApplyOutcome:
    source = _source(source_type, source_id, root)
    target = target or target_mod.detect_target(root=root)
    plan = plan_restore(
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
    app_plan = apps_mod.plan_installation(
        source.applications if install_apps else [],
        runner=runner,
        privilege=privilege,
        target=target,
        root=root,
        is_root=is_root,
    )
    report = restore_mod.execute(
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
    )
    outcome = ApplyOutcome(
        source_type=source_type,
        source_id=source_id,
        dry_run=not execute,
        plan=plan,
        install_plan=app_plan,
        report=report,
    )
    outcome.warnings = list(dict.fromkeys([*plan.warnings, *report.warnings]))
    return outcome


def apply_profile(
    profile_id: str,
    root: str = ".",
    execute: bool = False,
    approve: bool = False,
    home: str | Path | None = None,
    install_apps: bool = True,
    runner: Runner | None = None,
    privilege: str = "auto",
    refresh_index: bool = True,
    skip: Iterable[str] = (),
    progress: ProgressCallback = None,
    target: target_mod.Target | None = None,
    is_root: bool | None = None,
) -> ApplyOutcome:
    return _apply(
        "profile", profile_id, root, execute, approve, home, install_apps,
        runner, privilege, refresh_index, skip, progress, target, is_root,
    )


def apply_snapshot(
    snapshot_id: str,
    root: str = ".",
    execute: bool = False,
    approve: bool = False,
    home: str | Path | None = None,
    install_apps: bool = True,
    runner: Runner | None = None,
    privilege: str = "auto",
    refresh_index: bool = True,
    skip: Iterable[str] = (),
    progress: ProgressCallback = None,
    target: target_mod.Target | None = None,
    is_root: bool | None = None,
) -> ApplyOutcome:
    return _apply(
        "snapshot", snapshot_id, root, execute, approve, home, install_apps,
        runner, privilege, refresh_index, skip, progress, target, is_root,
    )


def preview(
    source_type: str,
    source_id: str,
    root: str = ".",
    runner: Runner | None = None,
    privilege: str = "auto",
    target: target_mod.Target | None = None,
) -> InstallPlan:
    """Vista de solo-aplicaciones (compatibilidad con `styler apps`)."""
    source = _source(source_type, source_id, root)
    return apps_mod.plan_installation(
        source.applications, runner=runner, privilege=privilege, target=target, root=root
    )
