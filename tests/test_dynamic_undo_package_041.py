"""Undo DAG dinámico para paquetes instalados por Apply."""
from __future__ import annotations

from pathlib import Path

from styler.changes.models import ChangeStatus
from styler.changes.service import ChangeService
from styler.receipts import ReceiptKind, StepReceipt, compile_rollback_workflow
from styler.runtime.commands import CommandResult
from styler.runtime.models import ExecutionContext, Status, StepDefinition
from styler.runtime.undo_executors import PackageUninstallExecutor
import styler.runtime.undo_executors as undo_module


def _receipt(**data) -> StepReceipt:
    return StepReceipt(
        receipt_id="pkg-1",
        change_id="photogimp",
        run_id="apply-1",
        step_id="app.gimp.install",
        step_type="install_package",
        kind=ReceiptKind.PACKAGE_INSTALLED,
        created_at=10.0,
        data=data,
    )


def test_undo_dag_uses_exact_manager_and_package_from_apply_receipt():
    workflow = compile_rollback_workflow([
        _receipt(manager="flatpak", package="org.gimp.GIMP", was_present=False)
    ])
    step = workflow.steps[0]
    assert step.step_type == "uninstall_package"
    assert step.config["manager"] == "flatpak"
    assert step.config["package"] == "org.gimp.GIMP"
    assert step.risk == "high"
    assert workflow.operation == "undo"


def test_uninstall_node_runs_after_all_filesystem_reversal():
    receipts = [
        _receipt(manager="flatpak", package="org.gimp.GIMP", was_present=False),
        StepReceipt(
            receipt_id="files-1", change_id="photogimp", run_id="apply-1",
            step_id="app.photogimp.install", step_type="install_overlay",
            kind=ReceiptKind.PATHS_WRITTEN, created_at=20.0,
            data={"created_paths": ["/home/u/.config/GIMP/3.0/plug-ins/x.py"]},
        ),
        StepReceipt(
            receipt_id="backup-1", change_id="photogimp", run_id="apply-1",
            step_id="app.photogimp.backup", step_type="backup_config",
            kind=ReceiptKind.BACKUP_CREATED, created_at=15.0,
            data={"source": "/home/u/.config/GIMP", "backup": "/home/u/.styler/b"},
        ),
    ]
    workflow = compile_rollback_workflow(receipts)
    package = next(step for step in workflow.steps if step.step_type == "uninstall_package")
    filesystem_ids = {
        step.id for step in workflow.steps
        if step.step_type in {"undo_remove_paths", "undo_restore_backup"}
    }
    assert set(package.needs) == filesystem_ids


def test_package_is_not_uninstalled_when_another_active_change_needs_it(tmp_path: Path):
    service = ChangeService(tmp_path / "library", tmp_path / "home")
    service._save_record(
        "other-change",
        {
            "status": ChangeStatus.INTEGRATED,
            "required_packages": [
                {"manager": "flatpak", "package": "org.gimp.GIMP"}
            ],
        },
    )
    protections = service._package_protections(excluding_change_id="photogimp")
    workflow = compile_rollback_workflow(
        [_receipt(manager="flatpak", package="org.gimp.GIMP", was_present=False)],
        package_protections=protections,
    )
    assert workflow.steps[0].config["protected_by_changes"] == ["other-change"]


def test_executor_uninstalls_with_registered_command_and_verifies(monkeypatch, tmp_path: Path):
    checks = iter([True, False])
    monkeypatch.setattr(
        PackageUninstallExecutor,
        "_probe_installed",
        staticmethod(lambda manager, package: next(checks)),
    )
    monkeypatch.setattr(
        PackageUninstallExecutor,
        "_uninstall_argv",
        staticmethod(lambda manager, package: ["flatpak", "uninstall", "-y", package]),
    )
    calls: list[list[str]] = []

    def fake_run(ctx, step, argv, **kwargs):
        calls.append(list(argv))
        return CommandResult(0, "removed", "", command=tuple(argv), log_path=str(tmp_path / "undo.log"))

    monkeypatch.setattr(undo_module, "run_step_command", fake_run)
    step = StepDefinition(
        id="undo.package",
        step_type="uninstall_package",
        config={
            "manager": "flatpak",
            "package": "org.gimp.GIMP",
            "was_present": False,
        },
    )
    ctx = ExecutionContext(root=tmp_path, dry_run=False).for_run("undo-1")
    ctx.artifacts_dir.mkdir(parents=True)
    result = PackageUninstallExecutor().run(step, ctx)
    assert result.success
    assert result.status == Status.ROLLED_BACK
    assert result.data["fully_reverted"] is True
    assert calls == [["flatpak", "uninstall", "-y", "org.gimp.GIMP"]]


def test_executor_preserves_package_protected_by_other_change(tmp_path: Path):
    step = StepDefinition(
        id="undo.package",
        step_type="uninstall_package",
        config={
            "manager": "flatpak",
            "package": "org.gimp.GIMP",
            "was_present": False,
            "protected_by_changes": ["image-workstation"],
        },
    )
    result = PackageUninstallExecutor().run(
        step, ExecutionContext(root=tmp_path, dry_run=False)
    )
    assert result.success
    assert result.status == Status.WAITING_FOR_USER
    assert result.data["fully_reverted"] is False


def test_package_that_existed_before_apply_never_gets_removed(tmp_path: Path):
    step = StepDefinition(
        id="undo.package",
        step_type="uninstall_package",
        config={
            "manager": "flatpak",
            "package": "org.gimp.GIMP",
            "was_present": True,
        },
    )
    result = PackageUninstallExecutor().run(
        step, ExecutionContext(root=tmp_path, dry_run=False)
    )
    assert result.success
    assert result.data["already_present_before_apply"] is True



def test_executor_does_not_claim_absence_when_manager_cannot_probe(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        PackageUninstallExecutor,
        "_probe_installed",
        staticmethod(lambda manager, package: None),
    )
    step = StepDefinition(
        id="undo.package",
        step_type="uninstall_package",
        config={"manager": "flatpak", "package": "org.gimp.GIMP", "was_present": False},
    )
    result = PackageUninstallExecutor().run(step, ExecutionContext(root=tmp_path, dry_run=False))
    assert not result.success
    assert result.data["error_code"] == "PACKAGE_STATUS_UNAVAILABLE"
