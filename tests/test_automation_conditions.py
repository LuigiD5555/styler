from __future__ import annotations

from styler.automation.conditions import (
    CallableCondition,
    ConditionAborted,
    StableValueCondition,
    wait_until,
)


def test_wait_until_returns_when_condition_becomes_true():
    calls = {"count": 0}
    clock = {"value": 0.0}

    def predicate():
        calls["count"] += 1
        return calls["count"] >= 3

    result = wait_until(
        CallableCondition("tercer intento", predicate),
        timeout_seconds=10,
        poll_interval_seconds=1,
        monotonic=lambda: clock["value"],
        sleeper=lambda seconds: clock.__setitem__("value", clock["value"] + seconds),
    )

    assert result.satisfied is True
    assert result.reason == "satisfied"
    assert result.attempts == 3
    assert result.elapsed_seconds == 2


def test_wait_until_distinguishes_timeout_from_abort():
    clock = {"value": 0.0}
    timeout = wait_until(
        CallableCondition("nunca", lambda: False),
        timeout_seconds=2,
        poll_interval_seconds=0.5,
        monotonic=lambda: clock["value"],
        sleeper=lambda seconds: clock.__setitem__("value", clock["value"] + seconds),
    )
    assert timeout.reason == "timeout"

    def aborted():
        raise ConditionAborted("el proceso terminó")

    result = wait_until(
        CallableCondition("proceso vivo", aborted),
        timeout_seconds=20,
    )
    assert result.satisfied is False
    assert result.reason == "aborted"
    assert "proceso terminó" in result.diagnostic


def test_stable_value_condition_requires_a_stable_interval():
    clock = {"value": 0.0}
    value = {"current": "a"}
    condition = StableValueCondition(
        "región estable",
        lambda: value["current"],
        stable_for_seconds=1,
        monotonic=lambda: clock["value"],
    )

    assert condition.evaluate() is False
    clock["value"] = 0.5
    assert condition.evaluate() is False
    value["current"] = "b"
    assert condition.evaluate() is False
    clock["value"] = 1.5
    assert condition.evaluate() is True
