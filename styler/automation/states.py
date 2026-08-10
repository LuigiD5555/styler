"""Máquina de estados para el ciclo de vida de una aplicación observada."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock

from .events import Event, EventBus, EventType


class AppState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    LOADING = "loading"
    READY = "ready"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class InvalidTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class TransitionRecord:
    previous: AppState
    event: EventType
    current: AppState


_TRANSITIONS: dict[tuple[AppState, EventType], AppState] = {
    (AppState.IDLE, EventType.START_REQUESTED): AppState.STARTING,
    (AppState.STARTING, EventType.PROCESS_STARTED): AppState.LOADING,
    (AppState.STARTING, EventType.APP_CRASHED): AppState.ERROR,
    (AppState.LOADING, EventType.APP_READY): AppState.READY,
    (AppState.LOADING, EventType.TIMEOUT): AppState.ERROR,
    (AppState.LOADING, EventType.APP_CRASHED): AppState.ERROR,
    (AppState.READY, EventType.APP_BUSY): AppState.EXECUTING,
    (AppState.READY, EventType.WORKFLOW_STARTED): AppState.EXECUTING,
    (AppState.EXECUTING, EventType.WORKFLOW_COMPLETED): AppState.VERIFYING,
    (AppState.EXECUTING, EventType.WORKFLOW_FAILED): AppState.RECOVERING,
    (AppState.EXECUTING, EventType.APP_CRASHED): AppState.RECOVERING,
    (AppState.VERIFYING, EventType.APP_READY): AppState.READY,
    (AppState.VERIFYING, EventType.WORKFLOW_FAILED): AppState.RECOVERING,
    (AppState.RECOVERING, EventType.RECOVERY_COMPLETED): AppState.READY,
    (AppState.RECOVERING, EventType.WORKFLOW_FAILED): AppState.ERROR,
    (AppState.ERROR, EventType.RECOVERY_STARTED): AppState.RECOVERING,
    (AppState.READY, EventType.STOP_REQUESTED): AppState.STOPPING,
    (AppState.ERROR, EventType.STOP_REQUESTED): AppState.STOPPING,
    (AppState.STOPPING, EventType.APP_STOPPED): AppState.STOPPED,
    (AppState.STOPPED, EventType.START_REQUESTED): AppState.STARTING,
}


class ApplicationStateMachine:
    """Responde únicamente en qué estado queda una aplicación tras un evento.

    No ejecuta acciones ni workflows. Esa separación evita convertir el DAG de
    Styler en una máquina de estados improvisada.
    """

    def __init__(
        self,
        bus: EventBus | None = None,
        initial: AppState = AppState.IDLE,
    ) -> None:
        self._state = initial
        self._history: list[TransitionRecord] = []
        self._lock = RLock()
        self._unsubscribe = bus.subscribe(self.consume) if bus else None

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    @property
    def history(self) -> tuple[TransitionRecord, ...]:
        with self._lock:
            return tuple(self._history)

    def consume(self, event: Event) -> AppState:
        with self._lock:
            key = (self._state, event.type)
            if key not in _TRANSITIONS:
                raise InvalidTransition(
                    f"El evento {event.type.value!r} no es válido mientras la aplicación "
                    f"está en {self._state.value!r}."
                )
            previous = self._state
            self._state = _TRANSITIONS[key]
            self._history.append(TransitionRecord(previous, event.type, self._state))
            return self._state

    def close(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
