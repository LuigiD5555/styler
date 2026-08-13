"""Eventos síncronos del subsistema de automatización.

El bus no conoce procesos, ventanas ni DAG. Solo transporta observaciones para
que una máquina de estados pueda decidir la transición correspondiente.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any, Callable


class EventType(str, Enum):
    START_REQUESTED = "start_requested"
    PROCESS_STARTED = "process_started"
    APP_READY = "app_ready"
    APP_BUSY = "app_busy"
    WORKFLOW_STARTED = "workflow_started"
    WORKFLOW_COMPLETED = "workflow_completed"
    WORKFLOW_FAILED = "workflow_failed"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"
    TIMEOUT = "timeout"
    APP_CRASHED = "app_crashed"
    STOP_REQUESTED = "stop_requested"
    APP_STOPPED = "app_stopped"


@dataclass(frozen=True)
class Event:
    type: EventType
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


Subscriber = Callable[[Event], None]


class EventBus:
    """Bus pequeño y síncrono con suscripciones removibles.

    Se mantiene síncrono para que las transiciones sean deterministas y fáciles
    de probar. Los observadores del sistema operativo pueden publicar desde otro
    hilo; el bus toma una instantánea de suscriptores y llama fuera del candado.
    """

    def __init__(self) -> None:
        self._subscribers: dict[EventType | None, list[Subscriber]] = {}
        self._lock = RLock()

    def subscribe(
        self,
        callback: Subscriber,
        event_type: EventType | None = None,
    ) -> Callable[[], None]:
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(callback)

        def unsubscribe() -> None:
            with self._lock:
                callbacks = self._subscribers.get(event_type, [])
                if callback in callbacks:
                    callbacks.remove(callback)

        return unsubscribe

    def publish(self, event: Event) -> None:
        with self._lock:
            callbacks = tuple(self._subscribers.get(event.type, ())) + tuple(
                self._subscribers.get(None, ())
            )
        for callback in callbacks:
            callback(event)
