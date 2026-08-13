from __future__ import annotations

from dataclasses import dataclass

from styler.automation.actions import ActionContext, DesktopClickAction, WaitAction
from styler.automation.conditions import CallableCondition, wait_until
from styler.automation.desktop import (
    DesktopElementCondition,
    ElementLocator,
    ElementSnapshot,
)
from styler.automation.specs import ActionSpec, BuildContext, ConditionSpec, default_action_registry


class FakeDriver:
    name = "fake"

    def __init__(self, snapshots=(), available=True):
        self.snapshots = tuple(snapshots)
        self.is_available = available
        self.clicked = False

    def available(self):
        return self.is_available

    def unavailable_reason(self):
        return "backend ausente"

    def find_all(self, locator):
        def matches(snapshot):
            return all((
                not locator.application or locator.application.lower() in snapshot.application.lower(),
                not locator.role or locator.role.lower() in snapshot.role.lower(),
                not locator.name or locator.name.lower() in snapshot.name.lower(),
                not locator.description or locator.description.lower() in snapshot.description.lower(),
            ))
        return tuple(item for item in self.snapshots if matches(item))

    def click(self, locator):
        self.clicked = True
        return self.snapshots[0]


def clickable_snapshot():
    return ElementSnapshot(
        application="GIMP",
        role="push button",
        name="Aceptar",
        description="",
        visible=True,
        enabled=True,
        focusable=True,
        actions=("click",),
    )


def test_semantic_element_condition_uses_role_name_and_state():
    driver = FakeDriver((clickable_snapshot(),))
    condition = DesktopElementCondition(
        driver,
        ElementLocator(application="GIMP", role="button", name="Aceptar"),
        expectation="clickable",
    )
    assert condition.evaluate()
    assert "matches=1" in condition.diagnostic()


def test_missing_desktop_backend_aborts_instead_of_spending_timeout():
    driver = FakeDriver(available=False)
    condition = DesktopElementCondition(
        driver, ElementLocator(name="Aceptar"), expectation="present"
    )
    result = wait_until(condition, timeout_seconds=30)
    assert not result.satisfied
    assert result.reason == "aborted"
    assert result.elapsed_seconds < 1
    assert "backend ausente" in result.diagnostic


class Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def test_wait_requires_success_to_remain_stable():
    clock = Clock()
    condition = CallableCondition("listo", lambda: True)
    result = wait_until(
        condition,
        timeout_seconds=5,
        poll_interval_seconds=0.25,
        success_stable_seconds=1.0,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    assert result.satisfied
    assert result.elapsed_seconds >= 1.0
    assert result.attempts >= 5
    assert result.history


def test_abort_condition_wins_before_timeout():
    clock = Clock()
    pending = CallableCondition("objetivo", lambda: False)
    error_visible = CallableCondition("diálogo de error", lambda: clock.value >= 0.5)
    result = wait_until(
        pending,
        timeout_seconds=20,
        poll_interval_seconds=0.25,
        abort_conditions=(error_visible,),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    assert result.reason == "aborted"
    assert result.elapsed_seconds == 0.5
    assert "diálogo de error" in result.diagnostic


def test_action_spec_round_trip_preserves_abort_conditions_and_stability():
    spec = ActionSpec(
        kind="wait_until",
        params={"timeout_seconds": 10, "success_stable_seconds": 1.5},
        condition=ConditionSpec("element_clickable", {"locator": {"name": "Aceptar"}}),
        abort_conditions=(
            ConditionSpec("element_present", {"locator": {"name": "Error"}}),
        ),
    )
    restored = ActionSpec.from_dict(spec.to_dict())
    assert restored == spec
    default_action_registry().validate(restored)


def test_desktop_click_is_declarative_and_dry_run_does_not_click():
    driver = FakeDriver((clickable_snapshot(),))
    spec = ActionSpec(
        kind="desktop_click",
        params={"locator": {"application": "GIMP", "role": "button", "name": "Aceptar"}},
    )
    action = default_action_registry().build(spec, BuildContext(desktop_driver=driver))
    preview = action.execute(ActionContext(dry_run=True))
    assert preview.success
    assert driver.clicked is False
    real = action.execute(ActionContext(dry_run=False))
    assert real.success
    assert driver.clicked is True


def test_runtime_graph_reuses_semantic_waits_and_desktop_click(tmp_path):
    from styler.runtime.engine import WorkflowEngine
    from styler.runtime.executors import ExecutorRegistry
    from styler.runtime.models import ExecutionContext, StepDefinition, WorkflowDefinition

    driver = FakeDriver((clickable_snapshot(),))
    workflow = WorkflowDefinition(
        name="desktop-flow",
        steps=[
            StepDefinition(
                id="wait-ready",
                step_type="wait_until",
                config={
                    "condition": {
                        "kind": "element_clickable",
                        "params": {"locator": {"application": "GIMP", "name": "Aceptar"}},
                    },
                    "abort_conditions": [
                        {
                            "kind": "element_present",
                            "params": {"locator": {"application": "GIMP", "name": "Error"}},
                        }
                    ],
                    "timeout_seconds": 2,
                    "poll_interval_seconds": 0.01,
                },
            ),
            StepDefinition(
                id="click-accept",
                step_type="desktop_click",
                needs=["wait-ready"],
                config={"locator": {"application": "GIMP", "name": "Aceptar"}},
            ),
        ],
    )
    run = WorkflowEngine(ExecutorRegistry.default()).run(
        workflow,
        ExecutionContext(
            root=tmp_path,
            dry_run=False,
            approve=True,
            values={"desktop_driver": driver, "home": tmp_path},
        ),
    )
    assert run.success
    assert driver.clicked is True
    assert [result.status for result in run.results] == ["ok", "ok"]


def test_runtime_graph_preview_never_clicks(tmp_path):
    from styler.runtime.engine import WorkflowEngine
    from styler.runtime.executors import ExecutorRegistry
    from styler.runtime.models import ExecutionContext, StepDefinition, WorkflowDefinition

    driver = FakeDriver((clickable_snapshot(),))
    workflow = WorkflowDefinition(
        name="preview-click",
        steps=[
            StepDefinition(
                id="click",
                step_type="desktop_click",
                config={"locator": {"application": "GIMP", "name": "Aceptar"}},
            )
        ],
    )
    run = WorkflowEngine(ExecutorRegistry.default()).run(
        workflow,
        ExecutionContext(
            root=tmp_path,
            dry_run=True,
            values={"desktop_driver": driver, "home": tmp_path},
        ),
    )
    assert run.success
    assert driver.clicked is False
    assert run.results[0].status == "dry_run"
