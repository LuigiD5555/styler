"""Combinadores de acciones y su forma declarativa."""
from __future__ import annotations

import pytest

from styler.automation.actions import (
    ActionContext,
    ActionResult,
    ForEachAction,
    NoteAction,
    RetryAction,
    SequenceAction,
    TimeoutAction,
    TryFinallyAction,
)
from styler.automation.specs import (
    ActionSpec,
    BuildContext,
    ConditionSpec,
    SpecError,
    UnknownKindError,
    default_action_registry,
    dumps,
    loads,
)


class FlakyAction:
    def __init__(self, succeed_on: int) -> None:
        self.name = "flaky"
        self.calls = 0
        self._succeed_on = succeed_on

    def execute(self, context: ActionContext) -> ActionResult:
        self.calls += 1
        return ActionResult(self.calls >= self._succeed_on, f"intento {self.calls}")


class FailingAction:
    name = "falla"

    def execute(self, context: ActionContext) -> ActionResult:
        return ActionResult(False, "no se pudo")


class RecordingAction:
    def __init__(self) -> None:
        self.name = "registro"
        self.seen: list = []

    def execute(self, context: ActionContext) -> ActionResult:
        self.seen.append(context.variables.get("item"))
        return ActionResult(True, "ok")


def test_retry_stops_at_the_first_success():
    action = FlakyAction(succeed_on=2)
    result = RetryAction(action, attempts=5, sleeper=lambda _: None).execute(ActionContext())
    assert result.success
    assert action.calls == 2


def test_retry_reports_the_last_failure():
    result = RetryAction(FailingAction(), attempts=3, sleeper=lambda _: None).execute(ActionContext())
    assert not result.success
    assert result.data["attempts"] == 3


def test_cleanup_runs_even_when_the_body_fails():
    cleanup = RecordingAction()
    result = TryFinallyAction("cerrar siempre", FailingAction(), cleanup).execute(ActionContext())
    assert not result.success
    assert cleanup.seen == [None]  # se ejecutó pese al fallo


def test_for_each_restores_the_previous_variable():
    action = RecordingAction()
    context = ActionContext(variables={"item": "previo"})
    ForEachAction("item", ["a", "b"], action).execute(context)
    assert action.seen == ["a", "b"]
    assert context.variables["item"] == "previo"


def test_timeout_publishes_a_deadline_and_restores_it():
    seen: list = []

    class Peek:
        name = "peek"

        def execute(self, context: ActionContext) -> ActionResult:
            seen.append(context.variables.get("deadline"))
            return ActionResult(True, "ok")

    context = ActionContext()
    TimeoutAction(Peek(), seconds=5.0).execute(context)
    assert seen[0] is not None
    assert "deadline" not in context.variables


def test_timeout_fails_when_the_limit_is_exceeded():
    ticks = iter([0.0, 10.0])
    result = TimeoutAction(
        NoteAction("lenta"), seconds=1.0, monotonic=lambda: next(ticks)
    ).execute(ActionContext())
    assert not result.success
    assert "plazo" in result.message


# ----------------------------------------------------------------- declarativo


def test_a_tree_of_specs_round_trips():
    spec = ActionSpec(
        kind="sequence",
        id="paso-01",
        title="Abrir y esperar",
        children=(
            ActionSpec(kind="launch_process", params={"argv": ["flatpak", "run", "org.gimp.GIMP"]}),
            ActionSpec(
                kind="wait_until",
                params={"timeout_seconds": 20.0},
                condition=ConditionSpec(
                    kind="path_glob", params={"base": "${HOME}/.config/GIMP", "pattern": "3.*"}
                ),
            ),
        ),
    )
    assert loads(dumps(spec)) == spec


def test_unknown_kind_is_rejected_instead_of_interpreted():
    registry = default_action_registry()
    with pytest.raises(UnknownKindError):
        registry.validate(ActionSpec(kind="shell", params={"command": "rm -rf ~"}))


def test_there_is_no_shell_kind_in_the_catalog():
    kinds = default_action_registry().known_kinds()
    assert not {"shell", "exec", "run_command", "python"} & kinds


def test_nested_unknown_kind_is_caught():
    registry = default_action_registry()
    spec = ActionSpec(kind="sequence", children=(ActionSpec(kind="inventado"),))
    with pytest.raises(UnknownKindError):
        registry.validate(spec)


def test_building_a_sleep_and_a_wait_are_different_actions(tmp_path):
    registry = default_action_registry()
    ctx = BuildContext(home=tmp_path)
    sleep = registry.build(ActionSpec(kind="sleep", params={"seconds": 1.0}), ctx)
    wait = registry.build(
        ActionSpec(
            kind="wait_until",
            params={"timeout_seconds": 3.0},
            condition=ConditionSpec(kind="path_exists", params={"path": "${HOME}/.config"}),
        ),
        ctx,
    )
    assert type(sleep).__name__ == "SleepAction"
    assert type(wait).__name__ == "WaitAction"


def test_missing_parameter_is_a_clear_error(tmp_path):
    registry = default_action_registry()
    with pytest.raises(SpecError):
        registry.build(ActionSpec(kind="sleep"), BuildContext(home=tmp_path))


def test_paths_are_confined_to_home(tmp_path):
    registry = default_action_registry()
    with pytest.raises(Exception):
        registry.build(
            ActionSpec(kind="wait_until", condition=ConditionSpec(
                kind="path_exists", params={"path": "/etc/shadow"})),
            BuildContext(home=tmp_path),
        )


def test_with_application_requires_a_known_profile(tmp_path):
    registry = default_action_registry()
    spec = ActionSpec(
        kind="with_application",
        params={"profile": "gimp", "argv": ["true"]},
        children=(ActionSpec(kind="note", params={"message": "hola"}),),
    )
    with pytest.raises(SpecError):
        registry.build(spec, BuildContext(home=tmp_path))


def test_sequence_of_specs_executes_in_order(tmp_path):
    registry = default_action_registry()
    action = registry.build(
        ActionSpec(
            kind="sequence",
            title="dos notas",
            children=(
                ActionSpec(kind="note", params={"message": "uno"}),
                ActionSpec(kind="note", params={"message": "dos"}),
            ),
        ),
        BuildContext(home=tmp_path),
    )
    result = action.execute(ActionContext(dry_run=True))
    assert result.success
    assert isinstance(action, SequenceAction)


def test_registered_application_is_resolved_by_local_context(tmp_path):
    registry = default_action_registry()
    action = registry.build(
        ActionSpec(kind="launch_application", params={"application_id": "demo"}),
        BuildContext(home=tmp_path, applications={"demo": ("true",)}),
    )
    assert type(action).__name__ == "LaunchProcessAction"


def test_unregistered_application_is_not_executable(tmp_path):
    registry = default_action_registry()
    with pytest.raises(SpecError):
        registry.build(
            ActionSpec(kind="launch_application", params={"application_id": "unknown"}),
            BuildContext(home=tmp_path, applications={}),
        )
