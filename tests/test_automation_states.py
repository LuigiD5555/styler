from __future__ import annotations

import pytest

from styler.automation.events import Event, EventBus, EventType
from styler.automation.states import AppState, ApplicationStateMachine, InvalidTransition


def test_lifecycle_wraps_a_finite_workflow():
    bus = EventBus()
    machine = ApplicationStateMachine(bus)
    for event_type in (
        EventType.START_REQUESTED,
        EventType.PROCESS_STARTED,
        EventType.APP_READY,
        EventType.WORKFLOW_STARTED,
        EventType.WORKFLOW_COMPLETED,
        EventType.APP_READY,
    ):
        bus.publish(Event(event_type, "test"))

    assert machine.state == AppState.READY
    assert [record.current for record in machine.history] == [
        AppState.STARTING,
        AppState.LOADING,
        AppState.READY,
        AppState.EXECUTING,
        AppState.VERIFYING,
        AppState.READY,
    ]


def test_invalid_transition_is_rejected():
    machine = ApplicationStateMachine()
    with pytest.raises(InvalidTransition):
        machine.consume(Event(EventType.WORKFLOW_STARTED, "test"))
