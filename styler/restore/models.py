from __future__ import annotations

from ._support import *  # noqa: F401,F403

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

@dataclass
class RestoreSource:
    """Lo que una configuración *pide*, con independencia de cómo se cumpla."""

    source_type: str
    source_id: str
    label: str
    environment_id: str                     # "kde-plasma" | "" (intención)
    applications: list[AppSpec] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)

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
    """Resultado de aplicar una configuración guardada.

    Esta vista vive junto al motor de restauración para evitar una fachada
    ``orchestrator`` separada que sólo reenviaba llamadas.
    """

    source_type: str
    source_id: str
    dry_run: bool
    plan: RestorePlan
    install_plan: Any
    report: RestoreReport | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def install_report(self):
        return self.report

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

ENVIRONMENT = "entorno"

PERSONALIZATION = "personalizacion"

ALL = "todo"

RESTORE_PIPELINES = (ENVIRONMENT, PERSONALIZATION, ALL)

@dataclass
class GateResult:
    open: bool
    reason: str = ""
    verification: Any | None = None
