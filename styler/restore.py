"""
styler.restore
==============
**El** orquestador. Un solo camino, un solo plan, un solo reporte.

Antes había dos caminos separados (`environment_restore` para KDE Plasma y otro
para aplicaciones y archivos). Aquí se unifican, porque restaurar un escritorio
es una sola cosa con un orden que no es negociable:

    escritorio → gestores y remotos → aplicaciones → VERIFICAR → archivos

La regla central del módulo, y la razón por la que existe:

    **Styler no copia ningún archivo de configuración hasta que el entorno y
    las aplicaciones necesarias estén instalados y verificados.**

Un panel de Plasma sin Plasma, o un `konsolerc` sin Konsole, no son una
restauración: son basura en el HOME de alguien.
"""
from __future__ import annotations

import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from styler import applications as apps_mod
from styler import catalogs
from styler import privileges
from styler import resolution as resolution_mod
from styler import target as target_mod
from styler import transaction as transaction_mod
from styler import verification as verify_mod
from styler.applications import AppSpec, ProgressCallback
from styler.models import FileEntry
from styler.parts import classify
from styler.resolution import Requirement, Resolution
from styler.resolvers import Candidate
from styler.runtime.commands import Runner, PipeCraftRunner

# --------------------------------------------------------------------------- #
# Estados (punto 10 del rediseño: distinguir resultados reales)
# --------------------------------------------------------------------------- #

class ItemStatus:
    ALREADY_PRESENT = "already_present"     # ya existía
    WILL_INSTALL = "will_install"           # se instalará
    WILL_ADD = "will_add"                   # remoto/repositorio a añadir
    WILL_UPDATE = "will_update"             # ya existe, se pedirá la versión más reciente
    INSTALLED = "installed"                 # instalado ahora
    UPDATED = "updated"                     # ya existía y se actualizó
    ADDED = "added"
    SKIPPED_BY_USER = "skipped_by_user"     # la persona lo omitió a propósito
    PENDING = "pending"                     # no se llegó a ejecutar
    MANUAL_REQUIRED = "manual_required"     # hay que hacerlo a mano
    MANAGER_MISSING = "manager_missing"
    UNSUPPORTED = "unsupported"
    PERMISSION_DENIED = "permission_denied"
    FAILED = "failed"
    VERIFICATION_FAILED = "verification_failed"


HUMAN = {
    ItemStatus.ALREADY_PRESENT: "Ya existente",
    ItemStatus.WILL_INSTALL: "Se instalará",
    ItemStatus.WILL_ADD: "Se añadirá",
    ItemStatus.WILL_UPDATE: "Se actualizará",
    ItemStatus.INSTALLED: "Instalado",
    ItemStatus.UPDATED: "Actualizado",
    ItemStatus.ADDED: "Añadido",
    ItemStatus.SKIPPED_BY_USER: "Omitido por ti",
    ItemStatus.PENDING: "Pendiente",
    ItemStatus.MANUAL_REQUIRED: "Requiere instalación manual",
    ItemStatus.MANAGER_MISSING: "Falta el gestor",
    ItemStatus.UNSUPPORTED: "No soportado",
    ItemStatus.PERMISSION_DENIED: "Permiso rechazado",
    ItemStatus.FAILED: "Fallido",
    ItemStatus.VERIFICATION_FAILED: "No se pudo verificar",
}

# Ninguno de estos estados es un éxito. Con cualquiera de ellos en un requisito
# obligatorio, no se toca ni un archivo del usuario (punto 5 del rediseño).
BLOCKING = {
    ItemStatus.MANAGER_MISSING,
    ItemStatus.UNSUPPORTED,
    ItemStatus.MANUAL_REQUIRED,
    ItemStatus.PERMISSION_DENIED,
    ItemStatus.FAILED,
    ItemStatus.VERIFICATION_FAILED,
    ItemStatus.PENDING,
}

SATISFIED = {
    ItemStatus.ALREADY_PRESENT,
    ItemStatus.INSTALLED,
    ItemStatus.UPDATED,
    ItemStatus.ADDED,
}

# Etapas de instalación, en orden. No se reordenan.
STAGE_DESKTOP = "escritorio"
STAGE_MANAGERS = "gestores"
STAGE_REMOTES = "remotos"
STAGE_REPOSITORIES = "repositorios"
STAGE_APPLICATIONS = "aplicaciones"
INSTALL_STAGES = (
    STAGE_DESKTOP,
    STAGE_MANAGERS,
    STAGE_REMOTES,
    STAGE_REPOSITORIES,
    STAGE_APPLICATIONS,
)

# Etapas de archivos, en orden (punto 8): primero el escritorio, al final los
# recursos personales. Nunca al revés.
FILE_STAGES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("plasma-base", "Configuración general de Plasma", ("tema-colores",)),
    ("paneles", "Paneles y widgets", ("paneles",)),
    ("atajos", "Atajos de teclado", ("atajos",)),
    ("apps-kde", "Konsole y Dolphin", ("konsole", "dolphin")),
    ("apps-config", "Configuración de aplicaciones", ("gimp", "aplicaciones-config")),
    ("recursos", "Fondos, iconos y recursos personales",
     ("iconos", "cursores", "fuentes", "fondos", "otros")),
)

_FILE_STAGE_BY_PART = {
    part: index
    for index, (_key, _title, parts) in enumerate(FILE_STAGES)
    for part in parts
}


# --------------------------------------------------------------------------- #
# Modelo del plan
# --------------------------------------------------------------------------- #

@dataclass
class RestoreItem:
    """Un requisito y su resolución **actual** en este equipo.

    El candidato se recalcula justo antes de ejecutar cada etapa: instalar
    Flatpak cambia lo que el equipo puede hacer, y el plan tiene que enterarse.
    """

    kind: str            # desktop | manager | remote | repository | application
    key: str
    title: str
    stage: str
    status: str
    requirement: Optional[Requirement] = None
    candidate: Optional[Candidate] = None
    detail: str = ""
    argv: list[str] = field(default_factory=list)
    app: Optional[AppSpec] = None
    mandatory: bool = True

    @property
    def pending_install(self) -> bool:
        return self.status in (ItemStatus.WILL_INSTALL, ItemStatus.WILL_ADD, ItemStatus.WILL_UPDATE)

    @property
    def blocking(self) -> bool:
        return self.mandatory and self.status in BLOCKING

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "title": self.title,
            "stage": self.stage,
            "status": self.status,
            "human_status": HUMAN.get(self.status, self.status),
            "detail": self.detail,
            "argv": list(self.argv),
            "resuelto_como": self.candidate.key if self.candidate else "",
            "mandatory": self.mandatory,
        }


@dataclass
class FileStage:
    key: str
    title: str
    entries: list[FileEntry] = field(default_factory=list)
    status: str = ItemStatus.PENDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "archivos": len(self.entries),
            "status": self.status,
            "human_status": HUMAN.get(self.status, self.status),
        }


@dataclass
class RestorePlan:
    """Todo lo que Styler hará, visible y aprobable de una sola vez (punto 9)."""

    source_type: str
    source_id: str
    target: target_mod.Target
    privilege: list[str] = field(default_factory=list)
    items: list[RestoreItem] = field(default_factory=list)
    file_stages: list[FileStage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def by_stage(self, stage: str) -> list[RestoreItem]:
        return [item for item in self.items if item.stage == stage]

    def pending(self) -> list[RestoreItem]:
        return [item for item in self.items if item.pending_install]

    def blocking(self) -> list[RestoreItem]:
        return [item for item in self.items if item.blocking]

    @property
    def can_apply(self) -> bool:
        return not self.blocking()

    @property
    def file_count(self) -> int:
        return sum(len(stage.entries) for stage in self.file_stages)

    def human_summary(self) -> list[str]:
        lines: list[str] = []
        pending = self.pending()
        if pending:
            lines.append("Se instalará:")
            lines.extend(f"  ✓ {item.title}" for item in pending)
        present = [item for item in self.items if item.status == ItemStatus.ALREADY_PRESENT]
        if present:
            lines.append("Ya está en este equipo:")
            lines.extend(f"  = {item.title}" for item in present)
        blocked = self.blocking()
        if blocked:
            lines.append("Falta resolver antes de continuar:")
            lines.extend(
                f"  ✗ {item.title} — {HUMAN.get(item.status, item.status)}: {item.detail}"
                for item in blocked
            )
        if self.file_stages:
            lines.append("Después se restaurarán:")
            lines.extend(
                f"  ✓ {stage.title} ({len(stage.entries)} archivos)"
                for stage in self.file_stages
            )
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "origen": {"tipo": self.source_type, "id": self.source_id},
            "destino": self.target.to_dict(),
            "requisitos": [item.to_dict() for item in self.items],
            "etapas_de_archivos": [stage.to_dict() for stage in self.file_stages],
            "puede_aplicarse": self.can_apply,
            "bloqueos": [item.title for item in self.blocking()],
            "advertencias": list(self.warnings),
            "resumen": self.human_summary(),
        }


@dataclass
class RestoreReport:
    plan: RestorePlan
    dry_run: bool
    started_at: float
    finished_at: float = 0.0
    verification: Optional[verify_mod.VerificationResult] = None
    files_applied: bool = False
    recovery_point: str = ""
    transaction_id: str = ""
    rollback_status: str = ""
    aborted_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    run_id: str = ""
    logs: dict[str, str] = field(default_factory=dict)
    environment_ready: bool = False   # pipeline 1 terminado y verificado

    # -- lectura de resultados ------------------------------------------
    def items_with(self, *statuses: str) -> list[RestoreItem]:
        return [item for item in self.plan.items if item.status in statuses]

    @property
    def installed(self) -> list[str]:
        return [item.title for item in self.items_with(ItemStatus.INSTALLED, ItemStatus.ADDED)]

    @property
    def updated(self) -> list[str]:
        return [item.title for item in self.items_with(ItemStatus.UPDATED)]

    @property
    def already_present(self) -> list[str]:
        return [item.title for item in self.items_with(ItemStatus.ALREADY_PRESENT)]

    @property
    def skipped(self) -> list[str]:
        return [item.title for item in self.items_with(ItemStatus.SKIPPED_BY_USER)]

    @property
    def failed(self) -> list[str]:
        return [
            item.title
            for item in self.items_with(
                ItemStatus.FAILED,
                ItemStatus.PERMISSION_DENIED,
                ItemStatus.VERIFICATION_FAILED,
                ItemStatus.MANAGER_MISSING,
                ItemStatus.UNSUPPORTED,
                ItemStatus.MANUAL_REQUIRED,
            )
        ]

    @property
    def pending(self) -> list[str]:
        return [item.title for item in self.items_with(ItemStatus.PENDING)]

    @property
    def ok(self) -> bool:
        if self.aborted_reason:
            return False
        if self.dry_run:
            return self.plan.can_apply
        if self.failed:
            return False
        # Correr solo el pipeline de entorno es un éxito legítimo: el sistema
        # quedó instalado y verificado, aunque no se hayan copiado archivos.
        return bool(self.files_applied or self.environment_ready)

    @property
    def needs_relogin(self) -> bool:
        """Plasma no relee su configuración solo: hay que reiniciar la sesión."""
        if not self.files_applied:
            return False
        return any(
            stage.status == ItemStatus.INSTALLED
            and stage.key in ("plasma-base", "paneles", "atajos")
            for stage in self.plan.file_stages
        ) or bool(self.installed)

    def human_message(self) -> str:
        if self.aborted_reason:
            return self.aborted_reason
        if self.dry_run:
            return "Plan calculado. Nada se instaló ni se escribió."
        if not self.ok:
            return "La restauración no terminó. Revisa los requisitos fallidos y reintenta."
        parts = []
        if self.installed:
            parts.append(f"{len(self.installed)} requisitos instalados")
        if self.already_present:
            parts.append(f"{len(self.already_present)} ya presentes")
        if self.files_applied:
            parts.append("archivos restaurados y verificados")
        return "Restauración completa: " + ", ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "simulacion": self.dry_run,
            "mensaje": self.human_message(),
            "plan": self.plan.to_dict(),
            "resultados": {
                "instalado": self.installed,
                "actualizado": self.updated,
                "ya_existente": self.already_present,
                "omitido_por_el_usuario": self.skipped,
                "pendiente": self.pending,
                "fallido": self.failed,
                "restaurado": [
                    stage.title
                    for stage in self.plan.file_stages
                    if stage.status == ItemStatus.INSTALLED
                ],
                "no_restaurado": [
                    stage.title
                    for stage in self.plan.file_stages
                    if stage.status != ItemStatus.INSTALLED
                ],
            },
            "verificacion": self.verification.to_dict() if self.verification else None,
            "punto_de_recuperacion": self.recovery_point,
            "transaccion": self.transaction_id,
            "estado_reversion": self.rollback_status,
            "advertencias": self.warnings,
            "reiniciar_sesion": self.needs_relogin,
            "reintentar": (
                "Puedes volver a ejecutar la restauración: lo ya instalado no se repite."
                if not self.ok and not self.dry_run
                else ""
            ),
        }


# --------------------------------------------------------------------------- #
# Construcción del plan
# --------------------------------------------------------------------------- #

@dataclass
class RestoreSource:
    """Lo que una configuración *pide*, con independencia de cómo se cumpla."""

    source_type: str
    source_id: str
    label: str
    environment_id: str                     # "kde-plasma" | "" (intención)
    applications: list[AppSpec] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)


def source_from_profile(profile_id: str, root: str = ".") -> RestoreSource:
    from styler.profiles import (
        compose,
        compose_applications,
        compose_profile,
        load_profile,
        load_profile_layers,
        unresolved_conflicts,
    )

    profile = load_profile(profile_id, root=root)
    layers = load_profile_layers(profile, root=root)
    applications = compose_applications(layers)

    # Un plan de componentes solo altera la restauración cuando la persona lo
    # confirmó. Un borrador sin confirmar sirve para explorar, nunca para
    # cambiar silenciosamente lo que se instalará o copiará.
    try:
        from styler.component_catalog.loader import load as load_component_catalog
        from styler.component_catalog.plan_draft import PlanDraftStore
        from styler.component_catalog.profile_bridge import (
            applications_for_draft,
            filter_layers_for_draft,
        )
        from styler.component_catalog.registry import ComponentRegistry

        draft = PlanDraftStore(root).load(profile_id)
        if draft is not None and draft.confirmed:
            registry = ComponentRegistry.from_report(load_component_catalog(root=root))
            original_layers = list(layers)
            layers = filter_layers_for_draft(original_layers, draft, registry)
            applications = applications_for_draft(original_layers, draft, registry)
    except (OSError, ValueError):
        # La validación visible del plan ya informa del problema. La lectura de
        # una configuración antigua no debe romperse por un archivo auxiliar.
        pass

    environment = ""
    for layer in layers:
        for record in layer.desktop_environments:
            environment = environment or record.environment_id
    # Con conflictos sin resolver todavía se puede *planear* (y verlos), pero la
    # capa de servicios no dejará aplicar hasta que se decidan.
    files = (
        compose(layers)
        if unresolved_conflicts(profile, layers)
        else compose_profile(profile, layers)
    )
    return RestoreSource(
        source_type="profile",
        source_id=profile_id,
        label=profile.name,
        environment_id=environment,
        applications=applications,
        files=files,
    )


def source_from_snapshot(snapshot_id: str, root: str = ".") -> RestoreSource:
    from styler.snapshot import load_snapshot

    snapshot = load_snapshot(snapshot_id, root=root)
    environment = ""
    for record in snapshot.state.desktop_environments:
        environment = environment or record.environment_id
    return RestoreSource(
        source_type="snapshot",
        source_id=snapshot_id,
        label=snapshot.label,
        environment_id=environment,
        applications=list(snapshot.state.applications),
        files=list(snapshot.state.files),
    )


# --------------------------------------------------------------------------- #
# Resolución de un requisito (se repite en cada etapa: replanificación)
# --------------------------------------------------------------------------- #

def _resolve(item: RestoreItem, target: target_mod.Target, runner: Runner, root: str) -> Resolution:
    """Vuelve a preguntar al equipo cómo satisfacer este requisito, AHORA."""
    requirement = item.requirement
    if requirement is None:
        return Resolution(reason="Requisito sin definir.")
    if requirement.kind == "desktop":
        capability = catalogs.cached(root).desktop(requirement.key.split(":", 1)[1])
        return resolution_mod.resolve_capability(capability, target, runner)
    if requirement.kind == "manager":
        capability = catalogs.cached(root).manager(requirement.key.split(":", 1)[1])
        return resolution_mod.resolve_capability(capability, target, runner)
    if requirement.kind == "application":
        return resolution_mod.resolve_application(requirement, target, runner, root)
    return Resolution(reason="")


def _plan_item(
    item: RestoreItem,
    target: target_mod.Target,
    runner: Runner,
    root: str,
    prefix: list[str],
    is_root: bool,
) -> RestoreItem:
    """Decide el estado y el comando de un requisito según el estado real del equipo."""
    if item.kind == "remote":
        remote = item.key.split(":", 1)[1]
        if remote in target_mod.configured_flatpak_remotes(runner):
            item.status = ItemStatus.ALREADY_PRESENT
            item.detail = "Ya configurado."
            item.argv = []
            return item
        argv = target_mod.remote_add_argv(remote, root)
        if argv is None:
            item.status = ItemStatus.MANUAL_REQUIRED
            item.detail = (
                "Styler solo añade remotos declarados como oficiales en el catálogo. "
                "Añade este a mano con «flatpak remote-add»."
            )
            item.argv = []
            return item
        item.status = ItemStatus.WILL_ADD
        item.detail = "Remoto oficial y firmado."
        item.argv = argv
        return item

    if item.kind == "repository":
        return item  # se decide en build_plan; Styler nunca instala llaves ajenas

    result = _resolve(item, target, runner, root)
    item.candidate = result.candidate

    if not result.resolved:
        reproducible = item.app.reproducible if item.app is not None else True
        item.status = (
            ItemStatus.MANAGER_MISSING
            if result.no_manager and reproducible
            else ItemStatus.MANUAL_REQUIRED
        )
        item.detail = result.reason
        item.argv = []
        return item

    candidate = result.candidate
    installed = resolution_mod.is_installed(candidate, runner)
    policy = item.requirement.version_policy if item.requirement else "present"

    if installed and policy != "latest":
        item.status = ItemStatus.ALREADY_PRESENT
        item.detail = f"Ya está en este equipo ({candidate.key})."
        item.argv = []
        return item

    if installed and policy == "latest":
        item.status = ItemStatus.WILL_UPDATE
        item.detail = f"Ya está ({candidate.key}); se pedirá la versión más reciente."
        item.argv = resolution_mod.upgrade_argv(candidate, prefix)
        return item

    argv = resolution_mod.install_argv(candidate, prefix)
    if not argv:
        item.status = ItemStatus.UNSUPPORTED
        item.detail = f"Styler no sabe instalar con «{candidate.manager}»."
        item.argv = []
        return item

    if candidate.privileged and not prefix and not is_root:
        item.status = ItemStatus.PERMISSION_DENIED
        item.detail = (
            "Hace falta permiso de administrador y no hay «sudo» ni «pkexec». "
            "Ejecuta Styler desde una terminal con sudo."
        )
        item.argv = []
        return item

    item.status = ItemStatus.WILL_INSTALL
    item.detail = result.reason
    item.argv = argv
    return item


# --------------------------------------------------------------------------- #
# Construcción del plan
# --------------------------------------------------------------------------- #

def apply_restorable_base(source: RestoreSource, root: str = ".") -> RestoreSource:
    """Suma la base personal restaurable a lo que trae el perfil (ver styler/base.py)."""
    from styler import base as base_mod

    applications, environment = base_mod.merge_into_source(source, root)
    source.applications = applications
    source.environment_id = environment
    return source


def build_plan(
    source: RestoreSource,
    root: str = ".",
    runner: Runner | None = None,
    target: target_mod.Target | None = None,
    privilege: str = "auto",
    skip: Iterable[str] = (),
    install_desktop: bool = True,
    apt_root: str = "/etc/apt",
    is_root: bool | None = None,
) -> RestorePlan:
    """Traduce la intención de una configuración en requisitos de ESTE equipo.

    Es una foto del estado actual. Al ejecutar, cada etapa se vuelve a resolver:
    instalar Flatpak cambia lo que el equipo sabe hacer.
    """
    import os

    runner = runner or PipeCraftRunner()
    target = target or target_mod.detect_target(root=root)
    catalog = catalogs.cached(root)
    source = apply_restorable_base(source, root)
    skipped = set(skip)
    root_now = (os.geteuid() == 0) if is_root is None else is_root
    prefix = apps_mod.privilege_prefix(runner, privilege, is_root)

    plan = RestorePlan(
        source_type=source.source_type,
        source_id=source.source_id,
        target=target,
        privilege=list(prefix),
    )
    if not target.known:
        plan.warnings.append(
            "No se reconoció la distribución de este equipo. Añádela a un catálogo de "
            "familias (~/.config/styler/catalog/) para que Styler pueda resolver paquetes."
        )

    # -- 1. Escritorio ------------------------------------------------------
    if source.environment_id and install_desktop:
        environment = source.environment_id
        capability = catalog.desktop(environment)
        title = capability.title if capability else f"Escritorio {environment}"
        check = verify_mod.verify_desktop(environment, runner, root)

        if capability is None or not capability.verifiable:
            # Desconocido NO es presente.
            plan.items.append(
                RestoreItem(
                    kind="desktop",
                    key=f"desktop:{environment}",
                    title=title,
                    stage=STAGE_DESKTOP,
                    status=ItemStatus.MANUAL_REQUIRED,
                    detail=check.detail,
                )
            )
        else:
            requirement = Requirement(
                kind="desktop",
                key=f"desktop:{environment}",
                title=title,
                identity=capability.identity,
                version_policy=capability.version_policy,
            )
            item = RestoreItem(
                kind="desktop",
                key=requirement.key,
                title=title,
                stage=STAGE_DESKTOP,
                status=ItemStatus.PENDING,
                requirement=requirement,
            )
            if check.ok and capability.version_policy != "latest":
                item.status = ItemStatus.ALREADY_PRESENT
                item.detail = check.detail
            else:
                _plan_item(item, target, runner, root, prefix, root_now)
                if check.ok and item.status == ItemStatus.WILL_INSTALL:
                    # Está en el sistema pero el gestor no lo ve: no reinstalar a ciegas.
                    item.status = ItemStatus.ALREADY_PRESENT
                    item.detail = check.detail
            plan.items.append(item)

    # -- 2. Gestores necesarios --------------------------------------------
    needed = sorted({
        app.manager for app in source.applications if catalog.manager(app.manager) is not None
    })
    for manager in needed:
        capability = catalog.manager(manager)
        program = target_mod.manager_binary(manager, root)
        if runner.available(program):
            plan.items.append(
                RestoreItem(
                    kind="manager",
                    key=f"manager:{manager}",
                    title=capability.title,
                    stage=STAGE_MANAGERS,
                    status=ItemStatus.ALREADY_PRESENT,
                    detail="Disponible.",
                )
            )
            continue
        requirement = Requirement(
            kind="manager", key=f"manager:{manager}", title=capability.title
        )
        item = RestoreItem(
            kind="manager",
            key=requirement.key,
            title=capability.title,
            stage=STAGE_MANAGERS,
            status=ItemStatus.PENDING,
            requirement=requirement,
        )
        plan.items.append(_plan_item(item, target, runner, root, prefix, root_now))

    # -- 3. Remotos de Flatpak ---------------------------------------------
    remotes = sorted({
        (app.remote or "flathub").lower()
        for app in source.applications
        if app.manager == "flatpak"
    })
    for remote in remotes:
        item = RestoreItem(
            kind="remote",
            key=f"remote:{remote}",
            title=f"Remoto {remote}",
            stage=STAGE_REMOTES,
            status=ItemStatus.PENDING,
        )
        plan.items.append(_plan_item(item, target, runner, root, prefix, root_now))

    # -- 4. Repositorios de terceros ---------------------------------------
    # Styler NO añade llaves ni repositorios de terceros: eso es confianza, no
    # automatización. Los declara y se detiene.
    seen: set[str] = set()
    for app in source.applications:
        if app.manager != "apt" or not app.remote_url or app.remote_url in seen:
            continue
        if not target_mod.is_third_party_apt(app.remote_url, root):
            continue
        seen.add(app.remote_url)
        key = f"repo:{app.remote_url}"
        configured = target_mod.apt_repository_configured(app.remote_url, apt_root)
        # Si el paquete ya está disponible por otra vía en este equipo, el
        # repositorio original deja de ser obligatorio.
        alternative = resolution_mod.resolve_application(
            apps_mod._requirement(app, root), target, runner, root
        )
        needed_here = target.family in ("ubuntu", "debian") and not alternative.resolved
        plan.items.append(
            RestoreItem(
                kind="repository",
                key=key,
                title=f"Repositorio {app.remote_url}",
                stage=STAGE_REPOSITORIES,
                status=ItemStatus.ALREADY_PRESENT if configured else ItemStatus.MANUAL_REQUIRED,
                detail=(
                    "Ya está configurado en este equipo."
                    if configured
                    else (
                        f"«{app.title}» vino de este repositorio, que no está configurado aquí. "
                        "Añádelo con su llave oficial; Styler no instala llaves de terceros por ti."
                    )
                ),
                mandatory=needed_here and key not in skipped,
            )
        )

    # -- 5. Aplicaciones ----------------------------------------------------
    for app in source.applications:
        key = f"app:{app.app_id}"
        if app.app_id in skipped or key in skipped:
            plan.items.append(
                RestoreItem(
                    kind="application",
                    key=key,
                    title=app.title,
                    stage=STAGE_APPLICATIONS,
                    status=ItemStatus.SKIPPED_BY_USER,
                    detail="La omitiste a propósito.",
                    app=app,
                    mandatory=False,
                )
            )
            continue
        requirement = apps_mod._requirement(app, root)
        item = RestoreItem(
            kind="application",
            key=key,
            title=app.title,
            stage=STAGE_APPLICATIONS,
            status=ItemStatus.PENDING,
            requirement=requirement,
            app=app,
        )
        _plan_item(item, target, runner, root, prefix, root_now)

        # Si su gestor todavía no existe pero SE VA A INSTALAR en una etapa
        # anterior, no es un bloqueo: es orden. El comando se calculará después,
        # cuando el gestor exista de verdad (replanificación por etapas).
        if item.status in (ItemStatus.MANAGER_MISSING, ItemStatus.MANUAL_REQUIRED) and any(
            other.kind == "manager" and other.pending_install for other in plan.items
        ):
            item.status = ItemStatus.WILL_INSTALL
            item.argv = []
            item.detail = "Se resolverá cuando su gestor esté instalado."
        plan.items.append(item)

    # -- 6. Archivos, por etapas -------------------------------------------
    grouped: dict[int, list[FileEntry]] = {}
    for entry in source.files:
        part = classify(entry.path).part_id
        index = _FILE_STAGE_BY_PART.get(part, len(FILE_STAGES) - 1)
        grouped.setdefault(index, []).append(entry)
    for index, (key, title, _parts) in enumerate(FILE_STAGES):
        entries = grouped.get(index, [])
        if entries:
            plan.file_stages.append(FileStage(key=key, title=title, entries=entries))

    return plan


def ordered_entries(plan: RestorePlan) -> list[FileEntry]:
    """Archivos en el orden de las etapas: escritorio primero, recursos al final."""
    result: list[FileEntry] = []
    for stage in plan.file_stages:
        result.extend(stage.entries)
    return result


# --------------------------------------------------------------------------- #
# Ejecución
# --------------------------------------------------------------------------- #

def execute(
    plan: RestorePlan,
    root: str = ".",
    home: str | Path | None = None,
    execute_real: bool = False,
    approve: bool = False,
    runner: Runner | None = None,
    refresh_index: bool = True,
    progress: ProgressCallback = None,
    label: str = "",
    target: target_mod.Target | None = None,
    privilege: str = "auto",
    is_root: bool | None = None,
    apply_files: bool = True,
) -> RestoreReport:
    """Ejecuta el plan **replanificando cada etapa** contra el estado real.

    Con `apply_files=False` corre solo el PIPELINE 1 (entorno): instala y
    verifica el sistema sin tocar ni un archivo del usuario.

    Instalar Flatpak cambia lo que el equipo puede hacer: por eso el comando de
    una aplicación de Flatpak no se calcula al principio, sino justo antes de
    ejecutarla, cuando su gestor ya existe.
    """
    import os

    runner = runner or PipeCraftRunner()
    target = target or plan.target or target_mod.detect_target(root=root)
    root_now = (os.geteuid() == 0) if is_root is None else is_root
    prefix = apps_mod.privilege_prefix(runner, privilege, is_root)
    run_id = f"restore-{uuid.uuid4().hex[:8]}"

    report = RestoreReport(
        plan=plan, dry_run=not execute_real, started_at=time.time(), run_id=run_id
    )
    report.warnings.extend(plan.warnings)

    if plan.blocking():
        report.aborted_reason = (
            "Faltan requisitos obligatorios: "
            + "; ".join(
                f"{item.title} ({HUMAN.get(item.status, item.status)})"
                for item in plan.blocking()
            )
            + ". Styler no copió ningún archivo. Resuélvelos y vuelve a intentarlo."
        )
        report.finished_at = time.time()
        return report

    if not execute_real:
        report.finished_at = time.time()
        return report

    if not approve:
        report.aborted_reason = (
            "Instalar programas y escribir archivos requiere tu aprobación explícita."
        )
        report.finished_at = time.time()
        return report

    logs_dir = Path(root) / ".styler" / "runs" / run_id / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Una restauración nueva limpia el estado de cancelación del intento anterior.
    begin = getattr(runner, "begin_operation", None)
    if callable(begin):
        begin()

    # ===================== PIPELINE 1: ENTORNO ============================== #
    # Instala y verifica el sistema. No toca ni un archivo del usuario.
    # Puede ejecutarse solo (`styler pipeline entorno`) y repetirse sin daño.

    # La persona autoriza UNA vez, fuera de la TUI. Styler nunca lee contraseñas.
    authorization_error = _authorize_privileged_commands(plan, runner, progress)
    if authorization_error:
        report.aborted_reason = authorization_error
        report.finished_at = time.time()
        return report

    # APT abre diálogos de debconf aunque se use «-y» (p. ej. elegir el gestor de
    # inicio de sesión). Se conserva el actual y se repara un dpkg interrumpido
    # antes de instalar nada.
    apt_error, apt_warnings, apt_logs = _prepare_apt_noninteractive(
        plan, runner, logs_dir, progress
    )
    report.warnings.extend(apt_warnings)
    report.logs.update(apt_logs)
    if apt_error:
        report.aborted_reason = apt_error
        report.finished_at = time.time()
        return report

    # Una instalación de escritorio completo dura más que el ticket de sudo (15
    # min por omisión). Sin esto, la persona autoriza, Styler empieza, y a mitad
    # de camino falla por «permiso rechazado» sin haber hecho nada mal.
    # El mantenedor usa el mismo runner: así se puede probar sin tocar el sistema.
    ticket = privileges.keepalive_for(
        prefix, run=lambda argv: runner.run(list(argv), timeout=15).returncode
    )
    if ticket is not None:
        ticket.start()
        report.warnings.append(
            "La autorización se mantendrá vigente durante la instalación; no tendrás "
            "que volver a escribir la contraseña."
        )

    try:
        _run_installation_stages(
            plan, report, runner, target, root, prefix, root_now,
            logs_dir, refresh_index, progress, ticket,
        )
    finally:
        if ticket is not None:
            ticket.stop()
            if ticket.lost:
                report.warnings.append(
                    "La autorización de administrador se perdió durante la instalación "
                    "(¿alguien ejecutó «sudo -k»?)."
                )

    return _finish_environment_and_files(
        plan, report, runner, root, home, label, progress, files=apply_files,
    )


def _run_installation_stages(
    plan: RestorePlan,
    report: RestoreReport,
    runner: Runner,
    target: target_mod.Target,
    root: str,
    prefix: list[str],
    root_now: bool,
    logs_dir: Path,
    refresh_index: bool,
    progress: ProgressCallback,
    ticket: "privileges.SudoTicket | None" = None,
) -> None:
    """Las etapas de instalación, replanificadas contra el equipo real."""
    if refresh_index:
        _refresh(plan, runner, prefix, logs_dir, target, root, progress)

    total = len(plan.pending())
    done = 0
    stopped = False

    for stage in INSTALL_STAGES:
        if stopped:
            break
        for item in plan.by_stage(stage):
            if item.status in (ItemStatus.ALREADY_PRESENT, ItemStatus.SKIPPED_BY_USER):
                continue

            # --- REPLANIFICACIÓN: el equipo cambió desde que se hizo el plan ---
            if item.kind != "repository":
                _plan_item(item, target, runner, root, prefix, root_now)

            if item.status in (ItemStatus.ALREADY_PRESENT, ItemStatus.SKIPPED_BY_USER):
                continue
            if item.blocking:
                stopped = True
                break
            if not item.pending_install:
                continue
            if not item.argv:
                # Nunca se ejecuta un comando vacío: eso es un fallo del plan.
                item.status = ItemStatus.MANUAL_REQUIRED
                item.detail = (
                    "Styler no supo con qué comando satisfacer este requisito en este equipo."
                )
                stopped = True
                break

            # La autorización se renueva antes de cada comando privilegiado: una
            # instalación larga nunca debe morir por un ticket caducado.
            if ticket is not None and item.argv[:1] == ["sudo"] and not ticket.ensure():
                item.status = ItemStatus.PERMISSION_DENIED
                item.detail = (
                    "La autorización de administrador ya no está vigente. "
                    "Vuelve a aplicar: lo ya instalado no se repite."
                )
                stopped = True
                break

            done += 1
            updating = item.status == ItemStatus.WILL_UPDATE
            apps_mod._emit(
                progress, "install", done, max(total, done),
                f"{stage}: {item.title}" + (" (actualizando)" if updating else ""),
            )
            log = logs_dir / f"{_slug(item.key)}.log"
            result = _run_observable(
                runner,
                item.argv,
                timeout=1800,
                progress=progress,
                current=done,
                total=max(total, done),
                title=item.title,
                log_path=log,
            )
            report.logs[item.key] = str(log)

            if result.returncode != 0:
                text = (result.stderr or result.stdout or "").lower()
                if result.returncode == 130:
                    item.status = ItemStatus.FAILED
                    item.detail = result.stderr or "La instalación fue cancelada de forma segura."
                    stopped = True
                    break
                if any(
                    marker in text
                    for marker in (
                        "could not get lock",
                        "unable to acquire the dpkg frontend lock",
                        "no se pudo obtener el bloqueo",
                        "lock-frontend",
                    )
                ):
                    item.status = ItemStatus.FAILED
                    item.detail = (
                        "Otro proceso está usando APT/dpkg. Styler esperó sin borrar "
                        "archivos de bloqueo. Cierra el Gestor de actualizaciones o espera "
                        "a que termine y vuelve a aplicar. No elimines /var/lib/dpkg/lock*. "
                        + _tail(result)
                    )
                elif "permission" in text or "password" in text or "sudo:" in text:
                    item.status = ItemStatus.PERMISSION_DENIED
                    item.detail = (
                        "No se obtuvo autorización de administrador. Styler no lee "
                        "contraseñas en la interfaz. Acepta el diálogo del sistema, o "
                        "cierra Styler y ejecuta «sudo -v && styler» en esta misma terminal."
                    )
                else:
                    item.status = ItemStatus.FAILED
                    item.detail = _tail(result)
                stopped = True
                break

            if item.kind == "remote":
                item.status = ItemStatus.ADDED
                item.detail = "Remoto añadido."
            elif updating:
                manager = item.candidate.manager if item.candidate else ""
                if resolution_mod.up_to_date(manager, result):
                    item.status = ItemStatus.ALREADY_PRESENT
                    item.detail = "Ya estaba en la versión más reciente."
                else:
                    item.status = ItemStatus.UPDATED
                    item.detail = "Actualizado a la versión más reciente del repositorio."
            else:
                item.status = ItemStatus.INSTALLED
                item.detail = f"Instalado ahora ({item.candidate.key if item.candidate else ''})."

            # --- verificación inmediata de la etapa ---
            check = _verify_item(item, runner, root)
            if check is not None and not check.ok:
                item.status = ItemStatus.VERIFICATION_FAILED
                item.detail = check.detail
                stopped = True
                break

    # Lo que quedó sin ejecutar es PENDIENTE, nunca un éxito.
    for item in plan.items:
        if item.pending_install:
            item.status = ItemStatus.PENDING
            item.detail = "No se llegó a ejecutar por un fallo anterior."


def _finish_environment_and_files(
    plan: RestorePlan,
    report: RestoreReport,
    runner: Runner,
    root: str,
    home: str | Path | None,
    label: str,
    progress: ProgressCallback,
    files: bool = True,
) -> RestoreReport:
    """Cierra el pipeline del entorno, abre la compuerta y corre el de archivos."""
    # Una cancelación es terminal para este intento: no se ejecutan verificadores
    # nuevos ni se copia ningún archivo.
    if bool(getattr(runner, "cancellation_requested", False)):
        report.aborted_reason = (
            "La restauración fue cancelada de forma segura. Styler detuvo el instalador "
            "y NO copió ningún archivo de configuración."
        )
        report.finished_at = time.time()
        return report

    # --- Verificación global antes de tocar archivos --------------------------
    apps_mod._emit(progress, "verify", 1, 1, "Verificando el entorno antes de restaurar")
    report.verification = verify_mod.verify_requirements(
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

    failed_items = [item for item in plan.items if item.blocking]
    if failed_items or not report.verification.ok:
        reasons = [
            f"{item.title} ({HUMAN.get(item.status, item.status)})" for item in failed_items
        ]
        reasons.extend(check.title for check in report.verification.failures())
        report.aborted_reason = (
            "El entorno no quedó completo: "
            + "; ".join(dict.fromkeys(reasons))
            + ". Styler NO copió ningún archivo de configuración. "
            "Corrige lo que falta y vuelve a ejecutar: lo ya instalado no se repite."
        )
        report.finished_at = time.time()
        return report

    # ===================== COMPUERTA ======================================== #
    # El entorno quedó instalado y verificado. Solo ahora se abre el pipeline de
    # personalización. Si no se pidió, el trabajo del entorno igual queda hecho.
    report.environment_ready = True
    if not files:
        report.finished_at = time.time()
        return report

    # ===================== PIPELINE 2: PERSONALIZACIÓN ====================== #
    # Transaccional: punto de recuperación, escritura por etapas y rollback.
    entries = ordered_entries(plan)
    if entries:
        apps_mod._emit(
            progress, "files", 1, 1, "Creando punto de recuperación y aplicando archivos"
        )
        run, record = transaction_mod.apply_entries_transactional(
            entries,
            source_type=plan.source_type,
            source_id=plan.source_id,
            root=root,
            execute=True,
            approve=True,
            label=label or plan.source_id,
            home=home,
        )
        report.recovery_point = record.backup_snapshot
        report.transaction_id = record.transaction_id
        report.rollback_status = record.rollback_status
        report.files_applied = bool(record.applied)
        for stage in plan.file_stages:
            stage.status = ItemStatus.INSTALLED if record.applied else ItemStatus.FAILED
        if not record.applied:
            report.aborted_reason = (
                "La escritura de archivos falló. "
                + (
                    "Styler restauró el estado anterior."
                    if record.rolled_back
                    else "ATENCIÓN: revisa el journal antes de continuar."
                )
            )
            report.warnings.append(record.error)
    else:
        report.files_applied = True

    if report.installed or report.updated:
        report.warnings.append(apps_mod.UNDO_DOES_NOT_UNINSTALL)
    if report.needs_relogin:
        report.warnings.append(
            "Cierra la sesión y vuelve a entrar para que el escritorio cargue la configuración."
        )
    report.finished_at = time.time()
    return report


def _verify_item(item: RestoreItem, runner: Runner, root: str):
    if item.kind == "desktop":
        return verify_mod.verify_desktop(item.key.split(":", 1)[1], runner, root)
    if item.kind == "manager":
        return verify_mod.verify_manager(item.key.split(":", 1)[1], runner, root)
    if item.kind == "remote":
        return verify_mod.verify_remote(item.key.split(":", 1)[1], runner, root)
    if item.kind == "application":
        return verify_mod.verify_candidate(
            item.title, item.candidate, runner, mandatory=item.mandatory, key=item.key
        )
    return None


def _refresh(
    plan: RestorePlan,
    runner: Runner,
    prefix: list[str],
    logs_dir: Path,
    target: target_mod.Target,
    root: str,
    progress: ProgressCallback = None,
) -> None:
    """Refresca el índice de TODOS los gestores implicados: apt, pacman, dnf y zypper."""
    managers = {
        item.candidate.manager for item in plan.pending() if item.candidate is not None
    }
    commands: list[tuple[str, list[str]]] = []
    for manager in sorted(managers):
        argv = resolution_mod.refresh_argv(manager, prefix)
        if argv:
            commands.append((f"Actualizar catálogo de {manager}", argv))
    for index, (title, argv) in enumerate(commands, start=1):
        _run_observable(
            runner,
            argv,
            timeout=600,
            progress=progress,
            current=index,
            total=max(1, len(commands)),
            title=title,
            log_path=logs_dir / f"refresh-{index}.log",
        )


def _tail(result) -> str:
    text = (result.stderr or result.stdout or "").strip().splitlines()
    return text[-1] if text else f"código {result.returncode}"


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value).strip("-").lower()


# ------------------------------------------------------------------ #
# Autorización, APT no interactivo y ejecución observable
# (capa de la 0.10.1–0.10.8: la persona autoriza una vez, ve la salida en
#  vivo y puede cancelar sin dejar dpkg bloqueado)
# ------------------------------------------------------------------ #


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SENSITIVE_FLAGS = {"--password", "--passwd", "--token", "--secret", "--auth"}

def _authorize_privileged_commands(
    plan: RestorePlan, runner: Runner, progress: ProgressCallback = None
) -> str:
    """Autoriza una sola vez sin compartir stdin con Textual.

    Los comandos ``sudo`` del plan son siempre no interactivos (``sudo -n``).
    ``pkexec`` usa el agente gráfico de PolicyKit. Si PolicyKit no puede
    autorizar, se intenta una credencial sudo ya validada, pero nunca se pide ni
    se canaliza una contraseña desde Styler.
    """
    privileged = [
        item for item in plan.pending()
        if item.argv and item.argv[0] in {"pkexec", "sudo"}
    ]
    if not privileged:
        return ""

    apps_mod._emit(
        progress, "authorize", 0, max(1, len(plan.pending())),
        "Esperando autorización del sistema",
    )

    uses_pkexec = any(item.argv[0] == "pkexec" for item in privileged)
    if uses_pkexec:
        result = runner.run(["pkexec", "true"], timeout=300)
        if result.returncode == 0:
            return ""

        # En una sesión sin agente gráfico, una autorización sudo previamente
        # validada sigue siendo segura porque no lee del terminal de la TUI.
        if runner.available("sudo"):
            sudo = runner.run(["sudo", "-n", "-v"], timeout=30)
            if sudo.returncode == 0:
                for item in privileged:
                    if item.argv and item.argv[0] == "pkexec":
                        item.argv = ["sudo", "-n", *item.argv[1:]]
                return ""

        detail = _tail(result)
        return (
            "No se pudo completar la autorización del sistema. Styler no copió "
            "ningún archivo. Acepta el diálogo de PolicyKit; si no aparece, cierra "
            "Styler y ejecuta «sudo -v && styler» en esta misma terminal. Styler "
            f"nunca rellena ni guarda tu contraseña. Detalle: {detail}"
        )

    result = runner.run(["sudo", "-n", "-v"], timeout=30)
    if result.returncode == 0:
        return ""
    return (
        "La instalación necesita autorización de administrador, pero sudo no tiene "
        "una credencial vigente. Styler no copió ningún archivo. Cierra Styler y "
        "ejecuta «sudo -v && styler» en esta misma terminal; escribe la contraseña "
        "antes de que se abra la interfaz. No ejecutes Styler completo como root."
    )


def _privilege_prefix_from_argv(argv: list[str]) -> list[str]:
    if argv[:2] == ["sudo", "-n"]:
        return ["sudo", "-n"]
    if argv[:1] == ["pkexec"]:
        return ["pkexec"]
    return []


def _current_display_manager(
    path: str | Path = "/etc/X11/default-display-manager",
) -> str:
    """Devuelve el gestor actual sin asumir LightDM, SDDM, GDM u otro nombre."""

    try:
        value = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    name = Path(value).name
    if not re.fullmatch(r"[A-Za-z0-9_.+\-]+", name):
        return ""
    return name


def _prepare_apt_noninteractive(
    plan: RestorePlan,
    runner: Runner,
    logs_dir: Path,
    progress: ProgressCallback = None,
) -> tuple[str, list[str], dict[str, str]]:
    """Prepara APT para una restauración sin preguntas invisibles.

    La selección del display manager no es una condición especial para KDE:
    es una pregunta genérica de paquetes Debian cuando ya existe otro gestor.
    Styler conserva dinámicamente el gestor que el equipo usa hoy. Si una
    instalación anterior dejó dpkg a medias, reanuda su configuración con la
    misma política no interactiva antes de instalar algo nuevo.
    """

    # Lo que importa es el gestor RESUELTO en este equipo, no el del equipo
    # original: un perfil de Mint restaurado en Arch no necesita nada de APT.
    apt_items = [
        item
        for item in plan.pending()
        if item.candidate is not None and item.candidate.manager == "apt"
    ]
    if not apt_items:
        return "", [], {}

    prefix = _privilege_prefix_from_argv(apt_items[0].argv)
    warnings: list[str] = []
    logs: dict[str, str] = {}
    total = max(1, len(plan.pending()))

    display_manager = _current_display_manager()
    if display_manager:
        if runner.available("debconf-set-selections"):
            seed_path = logs_dir / "display-manager.seed"
            seed_path.write_text(
                f"{display_manager} shared/default-x-display-manager "
                f"select {display_manager}\n",
                encoding="utf-8",
            )
            log_path = logs_dir / "prepare-display-manager.log"
            result = _run_observable(
                runner,
                [*prefix, "debconf-set-selections", str(seed_path)],
                timeout=60,
                progress=progress,
                current=0,
                total=total,
                title=f"Conservar {display_manager} como gestor de inicio de sesión",
                log_path=log_path,
            )
            logs["prepare:display-manager"] = str(log_path)
            if result.returncode != 0:
                warnings.append(
                    "No se pudo registrar previamente el gestor de inicio de sesión "
                    f"«{display_manager}». APT seguirá en modo no interactivo y "
                    "conservará la respuesta disponible en debconf."
                )
        else:
            warnings.append(
                f"Se detectó «{display_manager}» como gestor de inicio de sesión, "
                "pero no está disponible debconf-set-selections. APT seguirá en "
                "modo no interactivo."
            )

    # Un cierre forzado en medio de apt puede dejar paquetes desempaquetados.
    # Sólo se ejecuta dpkg --configure -a cuando dpkg --audit informa pendientes.
    if runner.available("dpkg"):
        audit = runner.run(["dpkg", "--audit"], timeout=30)
        audit_text = (audit.stdout + "\n" + audit.stderr).strip()
        if audit_text:
            log_path = logs_dir / "repair-dpkg.log"
            result = _run_observable(
                runner,
                apps_mod.dpkg_configure_argv(prefix),
                timeout=1800,
                progress=progress,
                current=0,
                total=total,
                title="Completar una instalación de paquetes interrumpida",
                log_path=log_path,
            )
            logs["prepare:dpkg"] = str(log_path)
            if result.returncode != 0:
                return (
                    "dpkg quedó incompleto y no pudo repararse automáticamente. "
                    "Styler no instaló más paquetes ni copió archivos. Revisa el "
                    f"registro: {log_path}",
                    warnings,
                    logs,
                )

    return "", warnings, logs


def _safe_command(argv: list[str]) -> str:
    """Comando legible sin exponer credenciales accidentales en pantalla o logs."""
    safe: list[str] = []
    redact_next = False
    for value in argv:
        if redact_next:
            safe.append("***")
            redact_next = False
            continue
        lower = value.lower()
        if lower in _SENSITIVE_FLAGS:
            safe.append(value)
            redact_next = True
            continue
        matched = next((flag for flag in _SENSITIVE_FLAGS if lower.startswith(flag + "=")), None)
        if matched:
            safe.append(value.split("=", 1)[0] + "=***")
            continue
        # URL con usuario:contraseña@host. Conserva el host para diagnosticar.
        value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1***:***@", value)
        safe.append(value)
    return shlex.join(safe)


def _display_line(value: str, limit: int = 900) -> str:
    value = _ANSI.sub("", value).replace("\x00", "")
    value = "".join(character for character in value if character == "\t" or ord(character) >= 32)
    return value[-limit:]


def _program_name(argv: list[str]) -> str:
    for value in argv:
        name = Path(value).name
        if (
            name in {"sudo", "pkexec", "env"}
            or value in {"-n", "--"}
            or value.startswith("-")
            or ("=" in value and not value.startswith(("/", "./")))
        ):
            continue
        return name
    return "instalador"


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _run_observable(
    runner: Runner,
    argv: list[str],
    *,
    timeout: float,
    progress: ProgressCallback,
    current: int,
    total: int,
    title: str,
    log_path: Path,
):
    """Ejecuta un paso dejando salida viva, latidos y un registro persistente."""
    safe_command = _safe_command(argv)
    program = _program_name(argv)
    started = time.monotonic()
    last_output_at = started
    log_path.parent.mkdir(parents=True, exist_ok=True)
    apps_mod._emit(progress, "command", current, total, f"{title}\n$ {safe_command}")

    with log_path.open("w", encoding="utf-8") as handle:
        handle.write(f"$ {safe_command}\n")
        handle.write(f"[inicio] {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        handle.flush()

        def output(line: str) -> None:
            nonlocal last_output_at
            clean = _display_line(line)
            if not clean.strip():
                return
            handle.write(clean + "\n")
            handle.flush()
            last_output_at = time.monotonic()
            apps_mod._emit(progress, "output", current, total, clean)

        def heartbeat(elapsed: float) -> None:
            quiet_for = max(0.0, time.monotonic() - last_output_at)
            quiet = (
                f" · sin salida nueva {_clock(quiet_for)}"
                if quiet_for >= 5
                else ""
            )
            apps_mod._emit(
                progress,
                "heartbeat",
                current,
                total,
                f"Proceso activo · {_clock(elapsed)} transcurridos · {program}{quiet}",
            )

        streaming = getattr(runner, "run_streaming", None)
        if callable(streaming):
            result = streaming(
                argv, timeout=timeout, on_output=output, on_heartbeat=heartbeat
            )
        else:
            # Compatibilidad con runners externos y dobles de prueba antiguos.
            result = runner.run(argv, timeout=timeout)
            for line in (result.stdout + "\n" + result.stderr).splitlines():
                output(line)

        elapsed = time.monotonic() - started
        handle.write(f"\n[fin] código={result.returncode} duración={_clock(elapsed)}\n")
        handle.flush()

    state = "terminó correctamente" if result.returncode == 0 else f"falló (código {result.returncode})"
    apps_mod._emit(
        progress, "command_done", current, total,
        f"{title}: {state} después de {_clock(elapsed)}",
    )
    apps_mod._emit(progress, "logfile", current, total, f"Registro completo: {log_path}")
    return result

