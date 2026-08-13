"""Selección explícita de métodos de ejecución para operaciones semánticas.

Styler no graba movimientos del ratón. Registra *qué operación* se necesita,
qué métodos conoce para realizarla y por qué eligió uno. La política normal es
terminal-first y conservadora: API nativa confinada, CLI registrada, D-Bus,
accesibilidad y, solo si se autoriza expresamente, entrada gráfica.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

from styler.runtime.models import StepDefinition, WorkflowDefinition


class Mechanism:
    NATIVE_API = "native_api"
    APPLICATION_CLI = "application_cli"
    REGISTERED_COMMAND = "registered_command"
    DBUS = "dbus"
    ACCESSIBILITY = "accessibility"
    GUI_INPUT = "gui_input"
    HUMAN = "human"


class Operation:
    PACKAGE_INSTALL = "package.install"
    PACKAGE_UNINSTALL = "package.uninstall"
    FILESYSTEM_BACKUP = "filesystem.backup"
    OVERLAY_APPLY = "overlay.apply"
    APPLICATION_INITIALIZE = "application.initialize"
    APPLICATION_VERIFY = "application.verify"
    APPLICATION_LAUNCH = "application.launch"
    APPLICATION_STOP = "application.stop"
    WAIT_OBSERVABLE = "wait.observable"
    WAIT_FIXED = "wait.fixed"
    HANDOFF_DOWNLOAD = "handoff.download"
    UNDO_RESTORE = "undo.restore"
    UNDO_REMOVE_EXACT = "undo.remove_exact"
    HUMAN_DECISION = "human.decision"
    NOTE = "note"


@dataclass(frozen=True)
class MethodPolicy:
    """Reglas para escoger entre métodos equivalentes."""

    prefer_terminal: bool = True
    allow_gui_input: bool = False
    require_reversible: bool = False
    allow_privileged: bool = True
    minimum_safety: int = 0


@dataclass(frozen=True)
class MethodContext:
    commands: frozenset[str] = frozenset()
    session_type: str = ""
    values: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def detect(cls, commands: Iterable[str] = ()) -> "MethodContext":
        names = set(commands)
        known = {
            "cp", "rsync", "tar", "flatpak", "dbus-send", "gdbus",
            "wmctrl", "xdotool", "kdotool", "rm", "mv", "curl",
        }
        names.update(name for name in known if shutil.which(name))
        return cls(frozenset(names))


@dataclass(frozen=True)
class OperationMethod:
    method_id: str
    operation: str
    label: str
    mechanism: str
    safety: int
    deterministic: bool = True
    reversible: bool = False
    requires_privilege: bool = False
    interactive: bool = False
    terminal_first: bool = True
    implemented: bool = True
    requires_commands: tuple[str, ...] = ()
    detail: str = ""

    def availability(self, context: MethodContext, policy: MethodPolicy) -> tuple[bool, str]:
        if not self.implemented:
            return False, "Método conocido, pero todavía no implementado."
        missing = [name for name in self.requires_commands if name not in context.commands]
        if missing:
            return False, f"Falta: {', '.join(missing)}."
        if self.mechanism == Mechanism.GUI_INPUT and not policy.allow_gui_input:
            return False, "La política terminal-first no permite entrada gráfica."
        if self.requires_privilege and not policy.allow_privileged:
            return False, "La política no permite operaciones privilegiadas."
        if policy.require_reversible and not self.reversible:
            return False, "La política exige un método reversible."
        if self.safety < policy.minimum_safety:
            return False, f"Seguridad {self.safety} menor al mínimo {policy.minimum_safety}."
        return True, "Disponible."

    def score(self, policy: MethodPolicy) -> int:
        score = int(self.safety)
        if self.deterministic:
            score += 8
        if self.reversible:
            score += 8
        if policy.prefer_terminal and self.terminal_first:
            score += 6
        if self.requires_privilege:
            score -= 10
        if self.interactive:
            score -= 14
        if self.mechanism == Mechanism.GUI_INPUT:
            score -= 35
        return score


@dataclass(frozen=True)
class MethodEvaluation:
    method: OperationMethod
    available: bool
    score: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "method_id": self.method.method_id,
            "label": self.method.label,
            "mechanism": self.method.mechanism,
            "safety": self.method.safety,
            "deterministic": self.method.deterministic,
            "reversible": self.method.reversible,
            "requires_privilege": self.method.requires_privilege,
            "interactive": self.method.interactive,
            "terminal_first": self.method.terminal_first,
            "implemented": self.method.implemented,
            "available": self.available,
            "score": self.score,
            "reason": self.reason,
            "detail": self.method.detail,
        }


@dataclass(frozen=True)
class MethodSelection:
    operation: str
    chosen: MethodEvaluation
    candidates: tuple[MethodEvaluation, ...]
    policy: MethodPolicy

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "chosen": self.chosen.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "policy": {
                "prefer_terminal": self.policy.prefer_terminal,
                "allow_gui_input": self.policy.allow_gui_input,
                "require_reversible": self.policy.require_reversible,
                "allow_privileged": self.policy.allow_privileged,
                "minimum_safety": self.policy.minimum_safety,
            },
        }


class MethodSelectionError(ValueError):
    pass


class MethodRegistry:
    def __init__(self) -> None:
        self._methods: dict[str, OperationMethod] = {}

    def register(self, method: OperationMethod) -> None:
        if method.method_id in self._methods:
            raise ValueError(f"Método duplicado: {method.method_id}")
        self._methods[method.method_id] = method

    def methods_for(self, operation: str) -> tuple[OperationMethod, ...]:
        return tuple(item for item in self._methods.values() if item.operation == operation)

    def select(
        self,
        operation: str,
        *,
        context: MethodContext | None = None,
        policy: MethodPolicy | None = None,
    ) -> MethodSelection:
        ctx = context or MethodContext.detect()
        rules = policy or MethodPolicy()
        methods = self.methods_for(operation)
        if not methods:
            raise MethodSelectionError(f"No hay métodos registrados para '{operation}'.")
        evaluations: list[MethodEvaluation] = []
        for method in methods:
            available, reason = method.availability(ctx, rules)
            evaluations.append(MethodEvaluation(method, available, method.score(rules), reason))
        evaluations.sort(
            key=lambda item: (item.available, item.score, item.method.safety, item.method.method_id),
            reverse=True,
        )
        chosen = next((item for item in evaluations if item.available), None)
        if chosen is None:
            details = "; ".join(f"{item.method.method_id}: {item.reason}" for item in evaluations)
            raise MethodSelectionError(f"No hay método disponible para '{operation}': {details}")
        return MethodSelection(operation, chosen, tuple(evaluations), rules)


STEP_OPERATIONS: dict[str, str] = {
    "install_package": Operation.PACKAGE_INSTALL,
    "uninstall_package": Operation.PACKAGE_UNINSTALL,
    "backup_config": Operation.FILESYSTEM_BACKUP,
    "install_overlay": Operation.OVERLAY_APPLY,
    "apply_config": Operation.OVERLAY_APPLY,
    "initialize_flatpak_app": Operation.APPLICATION_INITIALIZE,
    "verify": Operation.APPLICATION_VERIFY,
    "prepare_manual_handoff": Operation.HANDOFF_DOWNLOAD,
    "launch_application": Operation.APPLICATION_LAUNCH,
    "launch_process": Operation.APPLICATION_LAUNCH,
    "wait_until": Operation.WAIT_OBSERVABLE,
    "sleep": Operation.WAIT_FIXED,
    "undo_restore_backup": Operation.UNDO_RESTORE,
    "undo_remove_paths": Operation.UNDO_REMOVE_EXACT,
    "undo_note": Operation.HUMAN_DECISION,
    "note": Operation.NOTE,
}


def default_method_registry() -> MethodRegistry:
    registry = MethodRegistry()

    def add(method_id: str, operation: str, label: str, mechanism: str, safety: int, **kwargs: Any) -> None:
        registry.register(OperationMethod(method_id, operation, label, mechanism, safety, **kwargs))

    add(
        "package.registered-manager", Operation.PACKAGE_INSTALL,
        "Gestor de paquetes registrado", Mechanism.REGISTERED_COMMAND, 84,
        reversible=False, requires_privilege=True,
        detail="Usa argv construido por Styler; nunca una orden shell libre.",
    )
    add(
        "package.registered-uninstall", Operation.PACKAGE_UNINSTALL,
        "Desinstalación mediante el gestor registrado", Mechanism.REGISTERED_COMMAND, 86,
        reversible=False,
        detail=(
            "Usa el mismo gestor y paquete del recibo; no ejecuta autoremove, purge "
            "ni elimina datos de usuario por su cuenta."
        ),
    )
    add(
        "fs.python-backup", Operation.FILESYSTEM_BACKUP,
        "Copia nativa de Python", Mechanism.NATIVE_API, 99,
        reversible=True,
        detail="Copia confinada y recibo antes de sobrescribir.",
    )
    add(
        "fs.tar-backup", Operation.FILESYSTEM_BACKUP,
        "Archivo tar registrado", Mechanism.REGISTERED_COMMAND, 78,
        reversible=True, requires_commands=("tar",), implemented=False,
        detail="Alternativa futura para respaldos grandes.",
    )
    add(
        "overlay.python-scoped", Operation.OVERLAY_APPLY,
        "Overlay nativo confinado", Mechanism.NATIVE_API, 100,
        reversible=True,
        detail="Copia archivo por archivo, rechaza symlinks y emite recibos exactos.",
    )
    add(
        "overlay.rsync-scoped", Operation.OVERLAY_APPLY,
        "rsync con lista de cambios", Mechanism.REGISTERED_COMMAND, 80,
        reversible=True, requires_commands=("rsync",), implemented=False,
        detail="Alternativa futura; requiere reproducir recibos exactos.",
    )
    add(
        "app.registered-cli", Operation.APPLICATION_INITIALIZE,
        "CLI registrada de la aplicación", Mechanism.APPLICATION_CLI, 94,
        reversible=True,
        detail="Abre la aplicación con argv local conocido y supervisa el proceso.",
    )
    add(
        "app.dbus-activation", Operation.APPLICATION_INITIALIZE,
        "Activación D-Bus", Mechanism.DBUS, 88,
        reversible=True, requires_commands=("gdbus",), implemented=False,
    )
    add(
        "app.accessibility", Operation.APPLICATION_INITIALIZE,
        "Accesibilidad AT-SPI", Mechanism.ACCESSIBILITY, 66,
        reversible=True, terminal_first=False, implemented=False,
    )
    add(
        "app.gui-input", Operation.APPLICATION_INITIALIZE,
        "Entrada gráfica", Mechanism.GUI_INPUT, 25,
        deterministic=False, reversible=False, interactive=True,
        terminal_first=False, implemented=False,
    )
    add(
        "verify.native-probe", Operation.APPLICATION_VERIFY,
        "Comprobación nativa/CLI registrada", Mechanism.NATIVE_API, 96,
        reversible=True,
    )
    add(
        "wait.observable-condition", Operation.WAIT_OBSERVABLE,
        "Condición observable", Mechanism.NATIVE_API, 100,
        reversible=True,
        detail="Continúa al cumplirse la condición y aborta si queda imposible.",
    )
    add(
        "wait.observable-fallback-sleep", Operation.WAIT_OBSERVABLE,
        "Pausa fija de respaldo", Mechanism.NATIVE_API, 30,
        deterministic=False, reversible=True,
        detail="Método inferior: no demuestra que la aplicación esté lista.",
    )
    add(
        "wait.fixed-explicit", Operation.WAIT_FIXED,
        "Pausa fija explícita", Mechanism.NATIVE_API, 70,
        deterministic=True, reversible=True,
    )
    add(
        "app.registered-launch", Operation.APPLICATION_LAUNCH,
        "Lanzador registrado", Mechanism.APPLICATION_CLI, 95,
        reversible=True,
    )
    add(
        "app.process-terminate", Operation.APPLICATION_STOP,
        "Terminación controlada del proceso", Mechanism.NATIVE_API, 94,
        reversible=True,
    )
    add(
        "download.python-https", Operation.HANDOFF_DOWNLOAD,
        "Descarga HTTPS nativa", Mechanism.NATIVE_API, 90,
        reversible=True,
    )
    add(
        "download.curl-registered", Operation.HANDOFF_DOWNLOAD,
        "curl registrado", Mechanism.REGISTERED_COMMAND, 76,
        reversible=True, requires_commands=("curl",), implemented=False,
    )
    add(
        "undo.python-restore", Operation.UNDO_RESTORE,
        "Restauración exacta desde respaldo", Mechanism.NATIVE_API, 100,
        reversible=True,
    )
    add(
        "undo.python-remove-exact", Operation.UNDO_REMOVE_EXACT,
        "Eliminar únicamente efectos registrados", Mechanism.NATIVE_API, 100,
        reversible=True,
    )
    add(
        "undo.rm-registered", Operation.UNDO_REMOVE_EXACT,
        "rm registrado", Mechanism.REGISTERED_COMMAND, 45,
        reversible=False, requires_commands=("rm",), implemented=False,
    )
    add(
        "human.review", Operation.HUMAN_DECISION,
        "Decisión humana explícita", Mechanism.HUMAN, 100,
        deterministic=True, reversible=True, interactive=True,
        detail="Styler no adivina si debe desinstalar un paquete compartido.",
    )
    add(
        "note.no-effect", Operation.NOTE,
        "Nota sin efecto", Mechanism.NATIVE_API, 100,
        reversible=True,
    )
    return registry


def annotate_workflow_methods(
    workflow: WorkflowDefinition,
    *,
    registry: MethodRegistry | None = None,
    context: MethodContext | None = None,
    policy: MethodPolicy | None = None,
) -> WorkflowDefinition:
    """Selecciona y deja trazable el método de cada paso conocido."""
    methods = registry or default_method_registry()
    ctx = context or MethodContext.detect()
    rules = policy or MethodPolicy()
    annotated: list[StepDefinition] = []
    selections: dict[str, dict[str, Any]] = {}
    for step in workflow.steps:
        operation = step.operation or STEP_OPERATIONS.get(step.step_type, "")
        if not operation:
            annotated.append(step)
            continue
        selection = methods.select(operation, context=ctx, policy=rules)
        reason = (
            f"Se eligió {selection.chosen.method.label}: seguridad "
            f"{selection.chosen.method.safety}/100, mecanismo "
            f"{selection.chosen.method.mechanism}."
        )
        candidates = [item.to_dict() for item in selection.candidates]
        config = dict(step.config)
        semantic_sequence: list[dict[str, Any]] = []
        raw_sequence = config.get("semantic_operations") or []
        if isinstance(raw_sequence, list):
            for position, raw in enumerate(raw_sequence, 1):
                if not isinstance(raw, Mapping):
                    continue
                child_operation = str(raw.get("operation") or "")
                if not child_operation:
                    continue
                child_selection = methods.select(
                    child_operation, context=ctx, policy=rules
                )
                semantic_sequence.append({
                    "position": position,
                    "label": str(raw.get("label") or child_operation),
                    "operation": child_operation,
                    "method_id": child_selection.chosen.method.method_id,
                    "method_reason": (
                        f"Se eligió {child_selection.chosen.method.label}: seguridad "
                        f"{child_selection.chosen.method.safety}/100."
                    ),
                    "candidates": [item.to_dict() for item in child_selection.candidates],
                })
        if semantic_sequence:
            config["selected_semantic_sequence"] = semantic_sequence
        annotated_step = replace(
            step,
            config=config,
            operation=operation,
            method_id=selection.chosen.method.method_id,
            method_reason=reason,
            method_candidates=candidates,
        )
        annotated.append(annotated_step)
        selection_payload = selection.to_dict()
        if semantic_sequence:
            selection_payload["semantic_sequence"] = semantic_sequence
        selections[step.id] = selection_payload
    metadata = dict(workflow.metadata)
    metadata["method_policy"] = {
        "prefer_terminal": rules.prefer_terminal,
        "allow_gui_input": rules.allow_gui_input,
        "require_reversible": rules.require_reversible,
        "allow_privileged": rules.allow_privileged,
        "minimum_safety": rules.minimum_safety,
    }
    metadata["method_selections"] = selections
    return replace(workflow, steps=annotated, metadata=metadata)

# Contrato entre método seleccionado y ejecutor real. Las alternativas pueden
# mostrarse, pero el motor solo ejecuta un método que el step_type implementa.
EXECUTOR_METHODS: dict[str, frozenset[str]] = {
    "install_package": frozenset({"package.registered-manager"}),
    "backup_config": frozenset({"fs.python-backup"}),
    "install_overlay": frozenset({"overlay.python-scoped"}),
    "apply_config": frozenset({"overlay.python-scoped"}),
    "initialize_flatpak_app": frozenset({"app.registered-cli"}),
    "verify": frozenset({"verify.native-probe"}),
    "prepare_manual_handoff": frozenset({"download.python-https"}),
    "launch_application": frozenset({"app.registered-launch"}),
    "wait_until": frozenset({"wait.observable-condition"}),
    "sleep": frozenset({"wait.fixed-explicit"}),
    "undo_restore_backup": frozenset({"undo.python-restore"}),
    "undo_remove_paths": frozenset({"undo.python-remove-exact"}),
    "undo_note": frozenset({"human.review"}),
    "note": frozenset({"note.no-effect"}),
}


def validate_method_bindings(workflow: WorkflowDefinition) -> list[str]:
    errors: list[str] = []
    for step in workflow.steps:
        if not step.method_id:
            continue
        supported = EXECUTOR_METHODS.get(step.step_type)
        if supported is not None and step.method_id not in supported:
            errors.append(
                f"El paso '{step.id}' eligió '{step.method_id}', pero su ejecutor "
                f"'{step.step_type}' solo implementa: {', '.join(sorted(supported))}."
            )
    return errors
