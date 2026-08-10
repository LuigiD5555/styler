"""Controlador que separa abrir, esperar preparación y aplicar pausa adicional."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .actions import Action, ActionContext, ActionResult, SleepAction, WaitAction
from .events import Event, EventBus, EventType
from .profiles import ApplicationProfile
from .states import AppState, ApplicationStateMachine


@dataclass(frozen=True)
class LaunchReadyReport:
    launch: ActionResult
    readiness: ActionResult | None
    settle: ActionResult | None
    final_state: AppState
    elapsed_seconds: float

    @property
    def success(self) -> bool:
        return (
            self.launch.success
            and bool(self.readiness and self.readiness.success)
            and (self.settle is None or self.settle.success)
            and self.final_state == AppState.READY
        )


class ApplicationController:
    """Máquina jerárquica para una aplicación concreta.

    El DAG de Styler puede invocar este controlador dentro de uno de sus pasos,
    mientras la máquina de estados general conserva el estado del workflow.
    """

    def __init__(
        self,
        bus: EventBus | None = None,
        *,
        source: str,
        machine: ApplicationStateMachine | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus or EventBus()
        self.machine = machine or ApplicationStateMachine(self.bus)
        self.source = source
        self._monotonic = monotonic

    def wait_until_app_fully_loaded(
        self,
        profile: ApplicationProfile,
        context: ActionContext,
        *,
        on_poll: Callable[[int, float, str], None] | None = None,
    ) -> ActionResult:
        result = WaitAction(
            profile.fully_loaded_condition(),
            timeout_seconds=profile.startup_timeout_seconds,
            poll_interval_seconds=profile.poll_interval_seconds,
            on_poll=on_poll,
        ).execute(context)
        if result.success:
            self.bus.publish(Event(EventType.APP_READY, self.source, dict(result.data)))
        else:
            event_type = (
                EventType.APP_CRASHED
                if result.data.get("reason") in {"aborted", "error"}
                else EventType.TIMEOUT
            )
            self.bus.publish(Event(event_type, self.source, dict(result.data)))
        return result

    def settle_after_ready(
        self,
        profile: ApplicationProfile,
        context: ActionContext,
        *,
        elapsed_since_launch: float,
    ) -> ActionResult | None:
        additional = max(
            profile.settle_seconds,
            profile.minimum_runtime_seconds - elapsed_since_launch,
            0.0,
        )
        if additional <= 0:
            return None
        return SleepAction(additional).execute(context)

    def launch_wait_and_settle(
        self,
        launch_action: Action,
        profile: ApplicationProfile,
        context: ActionContext,
        *,
        on_poll: Callable[[int, float, str], None] | None = None,
    ) -> LaunchReadyReport:
        started = self._monotonic()
        self.bus.publish(Event(EventType.START_REQUESTED, self.source, {"application": profile.id}))
        launch_result = launch_action.execute(context)
        if not launch_result.success:
            self.bus.publish(
                Event(EventType.APP_CRASHED, self.source, {"message": launch_result.message})
            )
            return LaunchReadyReport(
                launch_result,
                None,
                None,
                self.machine.state,
                max(0.0, self._monotonic() - started),
            )

        self.bus.publish(Event(EventType.PROCESS_STARTED, self.source, dict(launch_result.data)))
        readiness = self.wait_until_app_fully_loaded(
            profile,
            context,
            on_poll=on_poll,
        )
        elapsed = max(0.0, self._monotonic() - started)
        if not readiness.success:
            return LaunchReadyReport(launch_result, readiness, None, self.machine.state, elapsed)

        settle = self.settle_after_ready(
            profile,
            context,
            elapsed_since_launch=elapsed,
        )
        return LaunchReadyReport(
            launch_result,
            readiness,
            settle,
            self.machine.state,
            max(0.0, self._monotonic() - started),
        )
