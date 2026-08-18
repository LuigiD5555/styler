"""Regresiones de autorización previa y errores de integración explicables."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from styler.changes import ChangeService
from styler.execution.processes import CommandResult, command_failure_summary
from styler.execution.executors import PackageInstallExecutor
from styler.planning.models import ExecutionContext, StepDefinition, WorkflowDefinition


def test_apt_plan_is_recognized_as_requiring_admin(monkeypatch, tmp_path):
    import styler.changes.service as module

    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    workflow = WorkflowDefinition(
        name="apt-demo",
        steps=[
            StepDefinition(
                "install.stacer",
                "install_package",
                config={"package": {"manager": "apt", "name": "stacer"}},
            )
        ],
    )
    service = ChangeService(tmp_path / "library", tmp_path / "home")
    assert service.plan_requires_admin(SimpleNamespace(workflow=workflow)) is True


def test_flatpak_plan_does_not_request_admin(monkeypatch, tmp_path):
    import styler.changes.service as module

    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    workflow = WorkflowDefinition(
        name="flatpak-demo",
        steps=[
            StepDefinition(
                "install.app",
                "install_package",
                config={"package": {"manager": "flatpak", "name": "org.demo.App"}},
            )
        ],
    )
    service = ChangeService(tmp_path / "library", tmp_path / "home")
    assert service.plan_requires_admin(SimpleNamespace(workflow=workflow)) is False


def test_package_install_failure_keeps_real_command_output(monkeypatch, tmp_path):
    import styler.execution.executors as module

    command = CommandResult(
        1,
        stdout="sudo: a password is required\n",
        command=("sudo", "-n", "apt-get", "install", "-y", "stacer"),
        log_path=str(tmp_path / "stacer.log"),
    )
    monkeypatch.setattr(module, "run_step_command", lambda *a, **k: command)
    monkeypatch.setattr(PackageInstallExecutor, "_install_argv", classmethod(lambda cls, manager, name: list(command.command)))
    monkeypatch.setattr(PackageInstallExecutor, "_is_installed", staticmethod(lambda manager, name: False))

    ctx = ExecutionContext(
        root=tmp_path,
        dry_run=False,
        approve=True,
        values={"change_id": "stacer"},
    )
    step = StepDefinition(
        "install.stacer",
        "install_package",
        config={"package": {"manager": "apt", "name": "stacer"}},
    )
    result = PackageInstallExecutor().run(step, ctx)

    assert result.success is False
    assert "mediante apt" in result.message
    assert "sudo: a password is required" in result.message
    assert result.data["error_code"] == "PACKAGE_INSTALL_FAILED"
    assert result.data["command"].startswith("sudo -n apt-get")
    assert result.data["artifact"].endswith("stacer.log")


def test_command_failure_summary_uses_only_useful_tail():
    result = CommandResult(1, stdout="\n".join(f"línea {i}" for i in range(20)))
    summary = command_failure_summary(result, max_lines=3)
    assert summary.splitlines() == ["línea 17", "línea 18", "línea 19"]


def test_review_screen_authorizes_before_opening_progress():
    source = Path("styler/tui/screens/changes.py").read_text(encoding="utf-8")
    start = source.index("class ChangeReviewScreen")
    end = source.index("class ChangeProgressScreen")
    block = source[start:end]
    assert "plan_requires_admin(self.plan)" in block
    assert "authorize_sudo_interactive()" in block
    assert block.index("authorize_sudo_interactive()") < block.index("ChangeProgressScreen(self.plan)")


def test_failure_result_exposes_diagnostic_path_and_partial_rollback_wording():
    source = Path("styler/tui/screens/changes.py").read_text(encoding="utf-8")
    start = source.index("class ChangeResultScreen")
    end = source.index("class ChangeBatchReviewScreen", start)
    block = source[start:end]
    assert "Diagnóstico técnico guardado en:" in block
    assert "Revertir lo alcanzado" in block
