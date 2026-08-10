"""
styler.applications
===================
El plano de **aplicaciones** de Styler: qué programas forman parte de una
configuración y cómo se vuelven a instalar en otra máquina.

Hasta 0.9.3 Styler observaba aplicaciones (`styler.provenance`) y sabía
ejecutar una instalación (`styler.runtime.executors`), pero nada conectaba
ambos planos: aplicar una configuración solo escribía archivos. Este módulo es
esa conexión, y es la única capa autorizada a instalar programas.

Reglas del módulo:

* **Idempotente.** Lo que ya está instalado no se vuelve a instalar.
* **Honesto.** Lo que no se puede reinstalar (AppImage suelto, paquete sin
  remote conocido) se declara como tal; nunca se adivina un origen.
* **Explícito.** Ejecutar requiere `execute=True` y `approve=True`. Un plan
  nunca toca el sistema.
* **Reversible solo en archivos.** Instalar un programa NO se deshace con el
  rollback de archivos, y eso se dice en el reporte en vez de ocultarse.
* **Probable.** Todo comando externo pasa por un `Runner` inyectable.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from styler.runtime.commands import CommandResult, FakeRunner, Runner, PipeCraftRunner  # noqa: F401

# --------------------------------------------------------------------------- #
# Estados de un paso
# --------------------------------------------------------------------------- #

class InstallStatus:
    ALREADY_INSTALLED = "already_installed"
    WILL_INSTALL = "will_install"
    MANAGER_MISSING = "manager_missing"
    MANUAL_REQUIRED = "manual_required"
    UNSUPPORTED = "unsupported"
    INSTALLED = "installed"
    FAILED = "failed"
    SKIPPED = "skipped"


HUMAN_STATUS = {
    InstallStatus.ALREADY_INSTALLED: "Ya está instalada",
    InstallStatus.WILL_INSTALL: "Se instalará",
    InstallStatus.MANAGER_MISSING: "Falta el gestor en este equipo",
    InstallStatus.MANUAL_REQUIRED: "Requiere instalación manual",
    InstallStatus.UNSUPPORTED: "Gestor no soportado todavía",
    InstallStatus.INSTALLED: "Instalada",
    InstallStatus.FAILED: "No se pudo instalar",
    InstallStatus.SKIPPED: "Omitida",
}

# Gestores que Styler sabe instalar hoy.
SUPPORTED_MANAGERS = ("apt", "flatpak", "snap", "pacman", "dnf", "zypper")
# Gestores que requieren privilegios de administrador.
PRIVILEGED_MANAGERS = ("apt", "snap", "pacman", "dnf", "zypper")

ProgressCallback = Optional[Callable[[str, int, int, str], None]]

# Una sola frase para una sola verdad, en todos los planos.
UNDO_DOES_NOT_UNINSTALL = (
    "Deshacer restaura tus archivos, pero no desinstala las aplicaciones."
)


def _emit(callback: ProgressCallback, stage: str, current: int, total: int, message: str) -> None:
    if callback:
        callback(stage, current, total, message)


# --------------------------------------------------------------------------- #
# Modelo
# --------------------------------------------------------------------------- #

@dataclass
class AppSpec:
    """Una aplicación que forma parte de una configuración.

    No es un paquete cualquiera de dpkg: es una aplicación que la persona
    instaló, con lo mínimo necesario para volver a pedirla en otra máquina.
    """

    manager: str              # gestor ORIGINAL: una preferencia, no una identidad
    name: str                 # nombre del paquete en el equipo original
    identity: str = ""        # identidad portátil (AppStream): org.kde.krita
    display_name: str = ""
    version: str = ""
    remote: str = ""          # flathub, jammy/main, snap store...
    remote_url: str = ""      # URL del repositorio de donde salió
    channel: str = ""         # rama de Flatpak / canal de Snap
    artifact_path: str = ""   # .deb / AppImage conservado localmente, si existe
    reason: str = "explicit"  # explicit | added-since-baseline | declared
    reproducible: bool = True  # ¿el gestor puede volver a entregarla?
    notes: str = ""

    @property
    def app_id(self) -> str:
        return f"{self.manager}:{self.name}"

    @property
    def title(self) -> str:
        return self.display_name or self.name

    @property
    def portable_id(self) -> str:
        """Lo que viaja entre distribuciones. El app_id es solo evidencia."""
        return self.identity or self.name

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AppSpec":
        return AppSpec(
            manager=str(data.get("manager", "")),
            name=str(data.get("name", "")),
            identity=str(data.get("identity", "") or ""),
            display_name=str(data.get("display_name", "") or ""),
            version=str(data.get("version", "") or ""),
            remote=str(data.get("remote", "") or ""),
            remote_url=str(data.get("remote_url", "") or ""),
            channel=str(data.get("channel", "") or ""),
            artifact_path=str(data.get("artifact_path", "") or ""),
            reason=str(data.get("reason", "explicit") or "explicit"),
            reproducible=bool(data.get("reproducible", True)),
            notes=str(data.get("notes", "") or ""),
        )


@dataclass
class InstallStep:
    app: AppSpec
    status: str
    argv: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def pending(self) -> bool:
        return self.status == InstallStatus.WILL_INSTALL

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app.app_id,
            "app": self.app.to_dict(),
            "status": self.status,
            "human_status": HUMAN_STATUS.get(self.status, self.status),
            "argv": list(self.argv),
            "message": self.message,
        }


@dataclass
class InstallPlan:
    steps: list[InstallStep] = field(default_factory=list)
    privilege: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def pending(self) -> list[InstallStep]:
        return [step for step in self.steps if step.pending]

    def blocked(self) -> list[InstallStep]:
        blocking = {
            InstallStatus.MANAGER_MISSING,
            InstallStatus.MANUAL_REQUIRED,
            InstallStatus.UNSUPPORTED,
        }
        return [step for step in self.steps if step.status in blocking]

    def already(self) -> list[InstallStep]:
        return [step for step in self.steps if step.status == InstallStatus.ALREADY_INSTALLED]

    @property
    def needs_nothing(self) -> bool:
        return not self.pending()

    def summary(self) -> str:
        if not self.steps:
            return "Esta configuración no incluye aplicaciones."
        parts = [
            f"{len(self.pending())} por instalar",
            f"{len(self.already())} ya presentes",
        ]
        if self.blocked():
            parts.append(f"{len(self.blocked())} no automatizables")
        return ", ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [step.to_dict() for step in self.steps],
            "privilege": list(self.privilege),
            "warnings": list(self.warnings),
            "summary": self.summary(),
        }


@dataclass
class InstallOutcome:
    app_id: str
    status: str
    message: str = ""
    argv: list[str] = field(default_factory=list)
    returncode: int = 0
    log_path: str = ""

    @property
    def success(self) -> bool:
        return self.status in (InstallStatus.INSTALLED, InstallStatus.ALREADY_INSTALLED)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["human_status"] = HUMAN_STATUS.get(self.status, self.status)
        data["success"] = self.success
        return data


@dataclass
class InstallReport:
    dry_run: bool
    started_at: float
    finished_at: float = 0.0
    outcomes: list[InstallOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    report_path: str = ""

    @property
    def installed(self) -> list[InstallOutcome]:
        return [item for item in self.outcomes if item.status == InstallStatus.INSTALLED]

    @property
    def failures(self) -> list[InstallOutcome]:
        return [item for item in self.outcomes if item.status == InstallStatus.FAILED]

    @property
    def success(self) -> bool:
        return not self.failures

    def human_message(self) -> str:
        if not self.outcomes:
            return "No había aplicaciones que instalar."
        if self.dry_run:
            return f"Simulación: {len(self.outcomes)} aplicaciones revisadas."
        if self.failures:
            names = ", ".join(item.app_id for item in self.failures)
            return f"No se pudieron instalar: {names}."
        if self.installed:
            return f"Se instalaron {len(self.installed)} aplicaciones."
        return "Todas las aplicaciones ya estaban instaladas."

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "dry_run": self.dry_run,
            "success": self.success,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "installed": [item.app_id for item in self.installed],
            "failed": [item.app_id for item in self.failures],
            "outcomes": [item.to_dict() for item in self.outcomes],
            "warnings": list(self.warnings),
            "message": self.human_message(),
        }


# --------------------------------------------------------------------------- #
# ¿De dónde salen las aplicaciones de una configuración?
# --------------------------------------------------------------------------- #

def applications_from_inventory(inventory, baseline=None, only_added: bool = False) -> list[AppSpec]:
    """Traduce un inventario de procedencia a la lista de aplicaciones «mías».

    Por omisión se toma **todo lo que la persona instaló a propósito**
    (`apt-mark showmanual`, Flatpak, Snap, AppImage), esté o no en la línea base.
    Una app que ya venía con la distro y que tú usas sigue siendo tuya: al
    restaurar en un equipo limpio hay que reinstalarla igual.

    La línea base solo sirve para *etiquetar* (`added-since-baseline`) y, si
    alguien lo pide explícitamente con `only_added=True`, para filtrar.
    """
    from styler.provenance.baseline import compare
    from styler.provenance.models import InstallReason

    if inventory is None:
        return []

    added_ids: set[str] = set()
    if baseline is not None:
        comparison = compare(baseline, inventory)
        added_ids = {record.app_id for record in comparison.added}
        added_ids.update(after.app_id for _before, after in comparison.changed)

    specs: list[AppSpec] = []
    for record in inventory.applications:
        if record.install_reason == InstallReason.DEPENDENCY:
            continue
        if only_added and baseline is not None and record.app_id not in added_ids:
            continue
        reason = "added-since-baseline" if record.app_id in added_ids else "explicit"
        specs.append(_spec_from_record(record, reason))
    return sorted(specs, key=lambda spec: (spec.manager, spec.name.lower()))


def _spec_from_record(record, reason: str) -> AppSpec:
    from styler.provenance.models import Confidence

    origin = record.origin
    reproducible = bool(record.reproducible_today) and record.manager in SUPPORTED_MANAGERS
    notes = ""
    if not reproducible:
        if record.manager not in SUPPORTED_MANAGERS:
            notes = f"Styler todavía no sabe instalar aplicaciones de «{record.manager}»."
        elif origin.confidence != Confidence.CONFIRMED:
            notes = "No hay un origen confirmado; habría que instalarla a mano."
    from styler.resolution import identity_for

    return AppSpec(
        manager=record.manager,
        name=record.name,
        identity=identity_for(record.manager, record.name),
        display_name=record.display_name or "",
        version=record.version or "",
        remote=origin.remote_name or "",
        remote_url=origin.remote_url or "",
        channel=origin.channel or origin.branch or "",
        artifact_path=record.integrity.artifact_path or "",
        reason=reason,
        reproducible=reproducible,
        notes=notes,
    )


def merge_applications(groups: Iterable[Iterable[AppSpec]]) -> list[AppSpec]:
    """Une listas de aplicaciones sin duplicar; gana la primera aparición."""
    merged: dict[str, AppSpec] = {}
    for group in groups:
        for spec in group or []:
            merged.setdefault(spec.app_id, spec)
    return sorted(merged.values(), key=lambda spec: (spec.manager, spec.name.lower()))


# --------------------------------------------------------------------------- #
# ¿Está instalada?
# --------------------------------------------------------------------------- #

def is_present(spec: AppSpec, runner: Runner, target=None, root: str = ".") -> bool | None:
    """¿Está esta aplicación en el equipo? None = el gestor no puede responder."""
    from styler import resolution, target as target_mod

    target = target or target_mod.detect_target(root=root)
    result = resolution.resolve_application(_requirement(spec, root), target, runner, root)
    if not result.resolved:
        return None
    return resolution.is_installed(result.candidate, runner)


def _requirement(spec: AppSpec, root: str = "."):
    from styler.resolution import Requirement, identity_for

    return Requirement(
        kind="application",
        key=f"app:{spec.app_id}",
        title=spec.title,
        identity=identity_for(spec.manager, spec.name, spec.identity, root),
        origin_manager=spec.manager,
        origin_name=spec.name,
        reproducible=spec.reproducible,
    )


# --------------------------------------------------------------------------- #
# Privilegios
# --------------------------------------------------------------------------- #

def privilege_prefix(runner: Runner, mode: str = "auto", is_root: bool | None = None) -> list[str]:
    """Elige un elevador que nunca lea la contraseña desde la TUI.

    `pkexec` se prefiere: la autorización ocurre en el diálogo de PolicyKit,
    separado del terminal que Textual está usando. `sudo` siempre lleva `-n`: o
    existe una credencial ya validada, o falla de inmediato. Styler nunca
    captura, rellena ni canaliza una contraseña (ver `styler/privileges.py`).
    """
    root = os.geteuid() == 0 if is_root is None else is_root
    if root or mode == "none":
        return []
    if mode in ("pkexec", "auto") and runner.available("pkexec"):
        return ["pkexec"]
    if mode in ("sudo", "auto") and runner.available("sudo"):
        return ["sudo", "-n"]
    return []


def privilege_available(runner: Runner, mode: str = "auto", is_root: bool | None = None) -> bool:
    root = os.geteuid() == 0 if is_root is None else is_root
    return root or bool(privilege_prefix(runner, mode, is_root))


# --------------------------------------------------------------------------- #
# APT no interactivo (de la 0.10.4/0.10.6): sin diálogos invisibles y esperando
# el bloqueo de dpkg en vez de romperlo.
# --------------------------------------------------------------------------- #

APT_NONINTERACTIVE_ENV = (
    "DEBIAN_FRONTEND=noninteractive",
    "DEBCONF_NONINTERACTIVE_SEEN=true",
    "APT_LISTCHANGES_FRONTEND=none",
    "NEEDRESTART_MODE=a",
)


def apt_install_argv(prefix: Iterable[str], *packages: str) -> list[str]:
    """Construye una instalación APT que nunca abra diálogos interactivos.

    ``-y`` solo responde a la confirmación de APT. No evita las preguntas de
    ``debconf`` (por ejemplo, elegir LightDM o SDDM) ni los conflictos de
    archivos de configuración. Por eso la política no interactiva y las
    opciones de dpkg forman parte del comando, independientemente del paquete.
    """

    return [
        *prefix,
        "env",
        *APT_NONINTERACTIVE_ENV,
        "apt-get",
        "-o",
        "Dpkg::Use-Pty=0",
        "-o",
        "DPkg::Lock::Timeout=300",
        "-o",
        "Dpkg::Options::=--force-confdef",
        "-o",
        "Dpkg::Options::=--force-confold",
        "-y",
        "install",
        *packages,
    ]


def apt_update_argv(prefix: Iterable[str]) -> list[str]:
    """Construye una actualización de catálogo APT sin interfaz interactiva."""

    return [
        *prefix,
        "env",
        *APT_NONINTERACTIVE_ENV,
        "apt-get",
        "-o",
        "Dpkg::Use-Pty=0",
        "-o",
        "DPkg::Lock::Timeout=300",
        "update",
    ]


def dpkg_configure_argv(prefix: Iterable[str]) -> list[str]:
    """Reanuda de forma segura una configuración de dpkg interrumpida."""

    return [
        *prefix,
        "env",
        *APT_NONINTERACTIVE_ENV,
        "dpkg",
        "--configure",
        "-a",
    ]


# --------------------------------------------------------------------------- #
# Plan (vista de solo-aplicaciones; el plan completo vive en styler.restore)
# --------------------------------------------------------------------------- #

def plan_installation(
    apps: Iterable[AppSpec],
    runner: Runner | None = None,
    privilege: str = "auto",
    is_root: bool | None = None,
    target=None,
    root: str = ".",
) -> InstallPlan:
    """Qué haría falta para tener estas aplicaciones EN ESTE equipo.

    El gestor original es una preferencia: si no existe aquí, se resuelve con el
    gestor nativo del destino o con Flatpak (ver `styler.resolution`).
    """
    from styler import resolution, target as target_mod

    runner = runner or PipeCraftRunner()
    target = target or target_mod.detect_target(root=root)
    prefix = privilege_prefix(runner, privilege, is_root)
    plan = InstallPlan(privilege=list(prefix))

    for spec in apps:
        result = resolution.resolve_application(_requirement(spec, root), target, runner, root)
        if not result.resolved:
            status = (
                InstallStatus.MANAGER_MISSING
                if result.no_manager and spec.reproducible
                else InstallStatus.MANUAL_REQUIRED
            )
            plan.steps.append(InstallStep(spec, status, message=result.reason))
            continue

        candidate = result.candidate
        if resolution.is_installed(candidate, runner):
            plan.steps.append(
                InstallStep(
                    spec,
                    InstallStatus.ALREADY_INSTALLED,
                    message=f"Ya está instalada ({candidate.key}).",
                )
            )
            continue

        argv = resolution.install_argv(candidate, prefix)
        if not argv:
            plan.steps.append(
                InstallStep(spec, InstallStatus.UNSUPPORTED, message=result.reason)
            )
            continue
        if candidate.privileged and not prefix and not (
            os.geteuid() == 0 if is_root is None else is_root
        ):
            plan.warnings.append(
                "Algunas aplicaciones necesitan permisos de administrador y no hay «sudo» "
                "ni «pkexec» disponibles. Ejecuta Styler desde una terminal con sudo."
            )
        plan.steps.append(
            InstallStep(spec, InstallStatus.WILL_INSTALL, argv=argv, message=result.reason)
        )

    manual = [step for step in plan.steps if step.status in (
        InstallStatus.MANUAL_REQUIRED, InstallStatus.MANAGER_MISSING
    )]
    if manual:
        plan.warnings.append(
            "Estas aplicaciones no se pueden resolver en este equipo: "
            + ", ".join(step.app.title for step in manual) + "."
        )
    plan.warnings = list(dict.fromkeys(plan.warnings))
    return plan


def execute_plan(
    plan: InstallPlan,
    root: str | Path = ".",
    execute: bool = False,
    approve: bool = False,
    runner: Runner | None = None,
    run_id: str = "",
    refresh_index: bool = False,
    progress: ProgressCallback = None,
) -> InstallReport:
    """Instala lo pendiente del plan. Sin `execute` y `approve` no toca nada."""
    runner = runner or PipeCraftRunner()
    report = InstallReport(dry_run=not execute, started_at=time.time())

    for step in plan.steps:
        if step.status != InstallStatus.WILL_INSTALL:
            report.outcomes.append(
                InstallOutcome(
                    app_id=step.app.app_id,
                    status=step.status,
                    message=step.message,
                )
            )
    report.warnings.extend(plan.warnings)

    pending = plan.pending()

    if not execute:
        for step in pending:
            report.outcomes.append(
                InstallOutcome(
                    app_id=step.app.app_id,
                    status=InstallStatus.WILL_INSTALL,
                    message=f"Se instalaría con: {' '.join(step.argv)}",
                    argv=list(step.argv),
                )
            )
        report.finished_at = time.time()
        return _persist(report, root, run_id)

    if not approve:
        for step in pending:
            report.outcomes.append(
                InstallOutcome(
                    app_id=step.app.app_id,
                    status=InstallStatus.SKIPPED,
                    message="Instalar programas requiere aprobación explícita.",
                    argv=list(step.argv),
                )
            )
        report.finished_at = time.time()
        return _persist(report, root, run_id)

    logs_dir = Path(root) / ".styler" / "runs" / (run_id or "apps") / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    if pending and refresh_index:
        _refresh_indexes(pending, plan.privilege, runner, logs_dir)

    total = len(pending)
    for index, step in enumerate(pending, start=1):
        _emit(progress, "install", index, total, f"Instalando {step.app.title}")
        result = runner.run(step.argv, timeout=1800)
        log_path = logs_dir / f"{_slug(step.app.app_id)}.log"
        log_path.write_text(
            f"$ {' '.join(step.argv)}\n\n[stdout]\n{result.stdout}\n[stderr]\n{result.stderr}\n",
            encoding="utf-8",
        )

        if result.returncode != 0:
            report.outcomes.append(
                InstallOutcome(
                    app_id=step.app.app_id,
                    status=InstallStatus.FAILED,
                    message=_human_error(result, step),
                    argv=list(step.argv),
                    returncode=result.returncode,
                    log_path=str(log_path),
                )
            )
            continue

        # Verificación posterior: no se declara instalado sin comprobarlo.
        present = is_present(step.app, runner, root=str(root))
        if present is False:
            report.outcomes.append(
                InstallOutcome(
                    app_id=step.app.app_id,
                    status=InstallStatus.FAILED,
                    message="El gestor terminó bien pero la aplicación no aparece instalada.",
                    argv=list(step.argv),
                    returncode=result.returncode,
                    log_path=str(log_path),
                )
            )
            continue

        report.outcomes.append(
            InstallOutcome(
                app_id=step.app.app_id,
                status=InstallStatus.INSTALLED,
                message="Instalada y verificada." if present else "Instalada.",
                argv=list(step.argv),
                log_path=str(log_path),
            )
        )

    if report.installed:
        report.warnings.append(UNDO_DOES_NOT_UNINSTALL)
    report.finished_at = time.time()
    return _persist(report, root, run_id)


def _refresh_indexes(
    pending: list[InstallStep], prefix: list[str], runner: Runner, logs_dir: Path
) -> None:
    from styler.resolution import refresh_argv

    managers = {
        step.argv[step.argv.index(program)] if program in step.argv else ""
        for step in pending
        for program in ("apt-get", "pacman", "dnf", "zypper")
    }
    lookup = {"apt-get": "apt", "pacman": "pacman", "dnf": "dnf", "zypper": "zypper"}
    commands = [
        argv
        for manager in sorted({lookup[name] for name in managers if name in lookup})
        if (argv := refresh_argv(manager, prefix))
    ]
    for argv in commands:
        result = runner.run(argv, timeout=600)
        (logs_dir / "refresh.log").write_text(
            f"$ {' '.join(argv)}\n{result.stdout}\n{result.stderr}\n", encoding="utf-8"
        )


def _human_error(result: CommandResult, step: InstallStep) -> str:
    text = (result.stderr or result.stdout or "").lower()
    if result.returncode == 124:
        return f"La instalación de {step.app.title} tardó demasiado y se detuvo."
    if "password" in text or "sudo:" in text or result.returncode == 1 and "permission" in text:
        return (
            f"Faltan permisos de administrador para instalar {step.app.title}. "
            "Ejecuta Styler desde una terminal con sudo."
        )
    if "unable to locate package" in text or "not found" in text or "no match" in text:
        return (
            f"El repositorio actual no ofrece «{step.app.name}». "
            "Puede que falte habilitar su origen en este equipo."
        )
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    tail = detail[-1] if detail else f"código {result.returncode}"
    return f"No se pudo instalar {step.app.title}: {tail}"


def _persist(report: InstallReport, root: str | Path, run_id: str) -> InstallReport:
    directory = Path(root) / ".styler" / "runs" / (run_id or "apps")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "applications.json"
        path.write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        report.report_path = str(path)
    except OSError:
        report.report_path = ""
    return report


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value).strip("-").lower()
