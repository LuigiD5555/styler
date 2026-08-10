"""Condiciones de tres estados y catálogo estilo `expected_conditions`."""
from __future__ import annotations

import pytest

from styler.automation.conditions import (
    AllCondition,
    AnyCondition,
    CallableCondition,
    CommandOutputCondition,
    ConditionState,
    CpuBelowCondition,
    DBusNameOwnedCondition,
    DirectoryQuiescentCondition,
    FileExistsCondition,
    FileStableCondition,
    GoneCondition,
    PathGlobCondition,
    ProcessAliveCondition,
    WindowPresentCondition,
    evaluate_state,
    wait_until,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_old_conditions_still_work_without_state(tmp_path):
    condition = FileExistsCondition(tmp_path / "no-existe")
    assert evaluate_state(condition) is ConditionState.PENDING
    (tmp_path / "no-existe").write_text("x")
    assert evaluate_state(condition) is ConditionState.SATISFIED


def test_dead_process_aborts_without_burning_the_timeout(tmp_path):
    clock = FakeClock()
    condition = ProcessAliveCondition(4242, proc_root=tmp_path)
    result = wait_until(
        condition,
        timeout_seconds=20.0,
        poll_interval_seconds=0.5,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    assert not result.satisfied
    assert result.reason == "aborted"
    assert result.elapsed_seconds == 0.0  # no esperó los 20 s


def test_process_without_pid_is_unsatisfiable():
    assert ProcessAliveCondition(lambda: None).state() is ConditionState.UNSATISFIABLE


def test_all_condition_propagates_unsatisfiable(tmp_path):
    combined = AllCondition(
        "gimp lista",
        (
            ProcessAliveCondition(4242, proc_root=tmp_path),
            CallableCondition("siempre", lambda: True),
        ),
    )
    assert combined.state() is ConditionState.UNSATISFIABLE


def test_any_condition_needs_all_children_unsatisfiable_to_abort(tmp_path):
    combined = AnyCondition(
        "alguna",
        (
            ProcessAliveCondition(4242, proc_root=tmp_path),
            CallableCondition("todavía no", lambda: False),
        ),
    )
    assert combined.state() is ConditionState.PENDING


def test_path_glob_generalizes_the_version(tmp_path):
    base = tmp_path / "GIMP"
    base.mkdir()
    condition = PathGlobCondition(base, "3.*", require_dir=True)
    assert condition.state() is ConditionState.PENDING
    (base / "3.2").mkdir()
    assert condition.state() is ConditionState.SATISFIED
    assert condition.matches() == (str(base / "3.2"),)


def test_gone_condition_inverts(tmp_path):
    lock = tmp_path / "app.lock"
    lock.write_text("x")
    condition = GoneCondition(FileExistsCondition(lock))
    assert condition.state() is ConditionState.PENDING
    lock.unlink()
    assert condition.state() is ConditionState.SATISFIED


def test_file_stable_waits_for_the_file_to_stop_growing(tmp_path):
    clock = FakeClock()
    target = tmp_path / "descarga.zip"
    target.write_text("a")
    condition = FileStableCondition(target, stable_for_seconds=2.0, monotonic=clock.monotonic)
    assert condition.state() is ConditionState.PENDING  # primera muestra
    target.write_text("aa")
    clock.now += 1.0
    assert condition.state() is ConditionState.PENDING  # cambió
    clock.now += 3.0
    condition.state()
    clock.now += 3.0
    assert condition.state() is ConditionState.SATISFIED


def test_directory_quiescent_detects_writes(tmp_path):
    clock = FakeClock()
    folder = tmp_path / "config"
    folder.mkdir()
    condition = DirectoryQuiescentCondition(folder, stable_for_seconds=1.0, monotonic=clock.monotonic)
    condition.state()
    clock.now += 2.0
    assert condition.state() is ConditionState.SATISFIED
    (folder / "nuevo.conf").write_text("x")
    assert condition.state() is ConditionState.PENDING


def test_missing_command_is_unsatisfiable_not_pending():
    condition = CommandOutputCondition(["comando-que-no-existe-en-ningun-equipo"], "algo")
    assert condition.state() is ConditionState.UNSATISFIABLE


def test_command_output_matches_pattern(monkeypatch):
    import styler.automation.conditions as module

    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/" + name)
    condition = CommandOutputCondition(
        ["flatpak", "info", "org.gimp.GIMP"],
        r"Version:\s*3",
        runner=lambda argv: (0, "Version: 3.0.4\n", ""),
    )
    assert condition.state() is ConditionState.SATISFIED


def test_dbus_condition_without_tools_is_unsatisfiable(monkeypatch):
    import styler.automation.conditions as module

    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    condition = DBusNameOwnedCondition("org.gimp.GIMP")
    assert condition.state() is ConditionState.UNSATISFIABLE
    assert "busctl" in condition.diagnostic()


def test_dbus_condition_detects_the_name(monkeypatch):
    import styler.automation.conditions as module

    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/busctl")
    condition = DBusNameOwnedCondition(
        "org.gimp.GIMP", runner=lambda argv: (0, "org.kde.kwin\norg.gimp.GIMP\n", "")
    )
    assert condition.state() is ConditionState.SATISFIED


def test_window_condition_without_tools_is_unsatisfiable(monkeypatch):
    import styler.automation.conditions as module

    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    assert WindowPresentCondition("GIMP").state() is ConditionState.UNSATISFIABLE


def test_cpu_below_needs_two_samples(tmp_path):
    clock = FakeClock()
    proc = tmp_path / "100"
    proc.mkdir()
    (proc / "stat").write_text("100 (gimp) S " + " ".join(["0"] * 8) + " 10 5 " + " ".join(["0"] * 30))
    condition = CpuBelowCondition(
        100, threshold_percent=5.0, stable_for_seconds=1.0,
        proc_root=tmp_path, monotonic=clock.monotonic,
    )
    assert condition.state() is ConditionState.PENDING
    clock.now += 2.0
    condition.state()
    clock.now += 2.0
    assert condition.state() is ConditionState.SATISFIED


def test_cpu_below_on_dead_process_is_unsatisfiable(tmp_path):
    assert CpuBelowCondition(999, proc_root=tmp_path).state() is ConditionState.UNSATISFIABLE
