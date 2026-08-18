"""Contratos mínimos para ejecutores de operaciones Styler."""
from __future__ import annotations

from abc import ABC, abstractmethod

from styler.planning.models import ExecutionContext, StepDefinition, StepResult


class StepExecutor(ABC):
    @property
    @abstractmethod
    def step_type(self) -> str:
        raise NotImplementedError

    def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        """Reconoce un efecto ya satisfecho sin volver a producirlo."""
        return None

    @abstractmethod
    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        raise NotImplementedError


class ExecutorRegistry:
    def __init__(self) -> None:
        self._executors: dict[str, StepExecutor] = {}

    def register(self, executor: StepExecutor) -> None:
        self._executors[executor.step_type] = executor

    def get(self, step_type: str) -> StepExecutor | None:
        return self._executors.get(step_type)

    def known_types(self) -> set[str]:
        return set(self._executors)


def emit_step_progress(
    ctx: ExecutionContext,
    step: StepDefinition,
    progress: float | None,
    operation: str,
    *,
    message: str = "",
) -> None:
    """Publica progreso sin acoplar ejecutores a una interfaz concreta."""
    callback = ctx.values.get("progress_callback")
    if not callable(callback):
        return
    try:
        callback({
            "step_id": step.id,
            "phase_progress": progress,
            "status": "running",
            "operation": operation,
            "message": message or operation,
        })
    except Exception:
        pass
