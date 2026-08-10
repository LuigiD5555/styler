"""Modelos públicos de la experiencia reinventada.

La interfaz ya no separa aplicaciones, archivos y escritorio. Todo lo que la
persona puede incorporar se representa como un cambio semántico con una o más
estrategias de integración.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class AutomationLevel:
    AUTOMATIC = "automatic"
    ASSISTED = "assisted"
    UNAVAILABLE = "unavailable"


class ChangeStatus:
    AVAILABLE = "available"
    REVERTING = "reverting"
    REVERTED = "reverted"
    PARTIALLY_REVERTED = "partially_reverted"
    PREPARING = "preparing"
    PREPARED = "prepared"
    INTEGRATING = "integrating"
    INTEGRATED = "integrated"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderOption:
    provider_id: str
    label: str
    description: str
    automation_level: str
    recommended: bool = False
    available: bool = True
    warning: str = ""


@dataclass(frozen=True)
class ChangeCard:
    change_id: str
    name: str
    description: str
    category: str
    status: str
    status_label: str
    provider_id: str = ""
    provider_label: str = ""
    automation_level: str = AutomationLevel.AUTOMATIC
    detail: str = ""
    warning: str = ""
    reversible: bool = False
    detected_at: float = 0.0
    continuation_available: bool = False


@dataclass(frozen=True)
class ChangeOption:
    """Opción avanzada de un cambio.

    Las opciones no son una pantalla aparte con casos especiales: entran al
    mismo compilador y modifican el DAG resultante. Lo que la persona eligió
    viaja en el registro del cambio, así que el rollback sabe qué se hizo.
    """

    option_id: str
    label: str
    description: str
    kind: str = "bool"  # bool | number | choice
    default: Any = True
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    advanced: bool = False

    def coerce(self, value: Any) -> Any:
        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in {0, 1}:
                return bool(value)
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"true", "1", "yes", "sí", "si", "on"}:
                    return True
                if normalized in {"false", "0", "no", "off"}:
                    return False
            raise ValueError(
                f"La opción '{self.option_id}' requiere un booleano explícito; recibió {value!r}."
            )
        if self.kind == "number":
            number = float(value)
            if self.minimum is not None:
                number = max(self.minimum, number)
            if self.maximum is not None:
                number = min(self.maximum, number)
            return number
        text = str(value)
        return text if not self.choices or text in self.choices else str(self.default)


@dataclass(frozen=True)
class ChangePhase:
    phase_id: str
    label: str
    description: str
    weight: float
    step_id: str
    determinate: bool = False


@dataclass
class ChangePlan:
    change_id: str
    name: str
    provider_id: str
    provider_label: str
    automation_level: str
    summary: str
    notice: str
    phases: tuple[ChangePhase, ...]
    workflow: Any = None
    undo_workflow: Any = None
    options: dict[str, Any] = field(default_factory=dict)
    reconciled_steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    continuation_mode: bool = False
    operation: str = "apply"  # apply | remove

    @property
    def workflows(self) -> ChangeWorkflowPair:
        return ChangeWorkflowPair(
            change_id=self.change_id,
            apply=self.workflow,
            undo=self.undo_workflow,
            undo_available=bool(self.undo_workflow and getattr(self.undo_workflow, "steps", ())),
        )

    @property
    def automatic(self) -> bool:
        return self.automation_level == AutomationLevel.AUTOMATIC




@dataclass(frozen=True)
class ChangeWorkflowPair:
    """Los dos DAG de un cambio.

    ``apply`` expresa cómo producir el cambio. ``undo`` se compila desde los
    efectos realmente registrados y puede no existir antes de una ejecución.
    No son el mismo grafo recorrido al revés.
    """

    change_id: str
    apply: Any
    undo: Any
    undo_available: bool
    undo_source: str = "receipts"

@dataclass(frozen=True)
class ChangeProgressEvent:
    change_id: str
    change_name: str
    phase_id: str
    phase_label: str
    operation: str
    phase_index: int
    phase_count: int
    phase_progress: float | None
    total_progress: float
    status: str
    message: str = ""
    event_type: str = "progress"
    terminal_line: str = ""
    command: str = ""
    pid: int | None = None
    elapsed_seconds: float = 0.0
    quiet_seconds: float = 0.0
    log_path: str = ""
    returncode: int | None = None


@dataclass(frozen=True)
class ChangeExecutionResult:
    change_id: str
    name: str
    ok: bool
    status: str
    title: str
    message: str
    provider_id: str
    provider_label: str
    automation_level: str
    report_path: str = ""
    handoff_path: str = ""
    instructions_path: str = ""
    details: tuple[str, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)
    diagnostic_path: str = ""
    operation: str = "apply"  # apply | remove


ProgressCallback = Callable[[ChangeProgressEvent], None] | None
