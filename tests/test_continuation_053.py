from __future__ import annotations

from pathlib import Path
import hashlib
import json

from styler.changes import ChangeService, ChangeStatus
from styler.component_catalog.executors import (
    BackupConfigExecutor,
    InitializeFlatpakAppExecutor,
    OverlayInstallExecutor,
)
from styler.receipts import ReceiptJournal, ReceiptKind
from styler.flatpak_facts import FlatpakApplicationFacts, save_flatpak_facts
from styler.runtime.engine import WorkflowEngine
from styler.runtime.executors import ExecutorRegistry, PackageInstallExecutor
from styler.runtime.models import ExecutionContext, Status, StepDefinition, WorkflowDefinition


def test_package_step_reuses_registered_installation_without_running_installer(monkeypatch, tmp_path: Path):
    executor = PackageInstallExecutor()
    monkeypatch.setattr(executor, "_is_installed", lambda manager, name: True)

    def should_not_install(*args, **kwargs):
        raise AssertionError("the installer must not be built for a reconciled package")

    monkeypatch.setattr(executor, "_install_argv", should_not_install)
    journal = ReceiptJournal(tmp_path, "photogimp")
    receipt = journal.record(
        run_id="old-run",
        step_id="app.gimp.install",
        step_type="install_package",
        kind=ReceiptKind.PACKAGE_INSTALLED,
        data={"manager": "flatpak", "package": "org.gimp.GIMP", "was_present": False},
    )
    registry = ExecutorRegistry()
    registry.register(executor)
    workflow = WorkflowDefinition(
        "continue-package",
        [
            StepDefinition(
                "app.gimp.install",
                "install_package",
                config={"package": {"manager": "flatpak", "name": "org.gimp.GIMP"}},
            )
        ],
    )
    run = WorkflowEngine(registry).run(
        workflow,
        ExecutionContext(
            root=tmp_path,
            dry_run=False,
            approve=True,
            values={"change_id": "photogimp", "continuation_mode": True},
        ),
    )

    assert run.success
    assert run.results[0].status == Status.RECONCILED
    assert run.results[0].data["receipt_id"] == receipt.receipt_id
    assert "no se volverá a instalar" in run.results[0].message or "reutilizará" in run.results[0].message


def test_failed_change_plan_explains_that_existing_gimp_will_be_reused(monkeypatch, tmp_path: Path):
    service = ChangeService(tmp_path / "library", tmp_path / "home")
    service._save_record("photogimp", {"status": ChangeStatus.FAILED, "provider_id": "flatpak"})
    monkeypatch.setattr(PackageInstallExecutor, "_is_installed", staticmethod(lambda manager, name: True))

    plan = service.build_plan("photogimp", "flatpak")

    assert plan.continuation_mode is True
    assert "app.gimp.install" in plan.reconciled_steps
    assert "no se volverá a instalar" in plan.summary
    phase = next(item for item in plan.phases if item.step_id == "app.gimp.install")
    assert phase.label == "Reutilizando GIMP ya instalado"


def test_initialized_gimp_is_reused_when_closed(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    version = home / ".var/app/org.gimp.GIMP/config/GIMP/3.0"
    version.mkdir(parents=True)
    save_flatpak_facts(
        tmp_path,
        FlatpakApplicationFacts(
            application_id="org.gimp.GIMP",
            installed=True,
            version="3.0.6",
            branch="stable",
            config_schema="3.0",
        ),
        config_root=str(version.parent),
        config_path=str(version),
        initialization_completed=True,
        initialized_application_version="3.0.6",
        initialized_config_schema="3.0",
        initialized_config_path=str(version),
    )
    step = StepDefinition(
        "app.gimp.initialize",
        "initialize_flatpak_app",
        config={
            "application_id": "org.gimp.GIMP",
            "config_root": "${HOME}/.var/app/org.gimp.GIMP/config/GIMP",
        },
    )
    executor = InitializeFlatpakAppExecutor()
    monkeypatch.setattr("styler.component_catalog.executors.shutil.which", lambda name: "/usr/bin/flatpak")
    monkeypatch.setattr(executor, "_flatpak_state", lambda app_id: (False, False, "not running"))

    result = executor.reconcile(step, ExecutionContext(root=tmp_path, values={"home": str(home)}))

    assert result is not None
    assert result.status == Status.RECONCILED
    assert result.data["config_path"] == str(version)


def test_continuation_reuses_valid_backup_receipt(tmp_path: Path):
    home = tmp_path / "home"
    backup = tmp_path / "backup" / "GIMP"
    backup.mkdir(parents=True)
    journal = ReceiptJournal(tmp_path, "photogimp")
    receipt = journal.record(
        run_id="old-run",
        step_id="app.photogimp.backup",
        step_type="backup_config",
        kind=ReceiptKind.BACKUP_CREATED,
        data={"source": str(home / ".config/GIMP"), "backup": str(backup), "existed": True},
    )
    step = StepDefinition(
        "app.photogimp.backup",
        "backup_config",
        config={"backup_source": "${HOME}/.config/GIMP"},
    )

    result = BackupConfigExecutor().reconcile(
        step,
        ExecutionContext(
            root=tmp_path,
            values={"home": str(home), "change_id": "photogimp", "continuation_mode": True},
        ),
    )

    assert result is not None
    assert result.status == Status.RECONCILED
    assert result.data["receipt_id"] == receipt.receipt_id


def test_overlay_marker_is_reused_only_when_continuing(tmp_path: Path):
    home = tmp_path / "home"
    target = home / ".var/app/org.gimp.GIMP/config/GIMP"
    target.mkdir(parents=True)
    version = target / "3.0"
    version.mkdir()
    config_file = version / "gimprc"
    config_file.write_text("photogimp", encoding="utf-8")
    marker = target / ".photogimp-marker"
    marker.write_text("source=test", encoding="utf-8")
    (target / ".photogimp-manifest.json").write_text(
        json.dumps({
            "target_config_version": "3.0",
            "files": {"gimprc": hashlib.sha256(b"photogimp").hexdigest()},
        }),
        encoding="utf-8",
    )
    step = StepDefinition(
        "app.photogimp.install",
        "install_overlay",
        config={"target": "${HOME}/.var/app/org.gimp.GIMP/config/GIMP"},
    )
    executor = OverlayInstallExecutor()

    fresh = executor.reconcile(step, ExecutionContext(root=tmp_path, values={"home": str(home)}))
    continued = executor.reconcile(
        step,
        ExecutionContext(root=tmp_path, values={"home": str(home), "continuation_mode": True}),
    )

    assert fresh is None
    assert continued is not None
    assert continued.status == Status.RECONCILED
    assert continued.data["marker"] == str(marker)


def test_execute_continues_failed_photogimp_without_repeating_completed_effects(monkeypatch, tmp_path: Path):
    from styler.component_catalog.executors import VerifyExecutor
    from styler.runtime.models import StepResult

    root = tmp_path / "library"
    home = tmp_path / "home"
    config_root = home / ".config/GIMP"
    version = config_root / "3.0"
    version.mkdir(parents=True)
    (home / ".var/app/org.gimp.GIMP/config/GIMP/3.0").mkdir(parents=True)
    config_file = version / "gimprc"
    config_file.write_text("photogimp", encoding="utf-8")
    (config_root / ".photogimp-marker").write_text("source=test", encoding="utf-8")
    (config_root / ".photogimp-manifest.json").write_text(
        json.dumps({
            "target_config_version": "3.0",
            "files": {"gimprc": hashlib.sha256(b"photogimp").hexdigest()},
        }),
        encoding="utf-8",
    )
    save_flatpak_facts(
        root,
        FlatpakApplicationFacts(
            application_id="org.gimp.GIMP",
            installed=True,
            version="3.0.6",
            branch="stable",
            config_schema="3.0",
        ),
        config_root=str(home / ".var/app/org.gimp.GIMP/config/GIMP"),
        config_path=str(home / ".var/app/org.gimp.GIMP/config/GIMP/3.0"),
        initialization_completed=True,
        initialized_application_version="3.0.6",
        initialized_config_schema="3.0",
        initialized_config_path=str(home / ".var/app/org.gimp.GIMP/config/GIMP/3.0"),
    )

    service = ChangeService(root, home)
    service._save_record("photogimp", {"status": ChangeStatus.FAILED, "provider_id": "flatpak"})
    journal = ReceiptJournal(root, "photogimp")
    journal.record(
        run_id="old-run",
        step_id="app.gimp.install",
        step_type="install_package",
        kind=ReceiptKind.PACKAGE_INSTALLED,
        data={"manager": "flatpak", "package": "org.gimp.GIMP", "was_present": False},
    )
    backup = root / "old-backup" / "GIMP"
    backup.mkdir(parents=True)
    journal.record(
        run_id="old-run",
        step_id="app.photogimp.backup",
        step_type="backup_config",
        kind=ReceiptKind.BACKUP_CREATED,
        data={"source": str(version), "backup": str(backup), "existed": True},
    )

    monkeypatch.setattr(PackageInstallExecutor, "_is_installed", staticmethod(lambda manager, name: True))
    monkeypatch.setattr(
        "styler.component_catalog.executors.inspect_flatpak_application",
        lambda app_id: FlatpakApplicationFacts(
            application_id=app_id,
            installed=True,
            version="3.0.6",
            branch="stable",
            config_schema="3.0",
        ),
    )
    monkeypatch.setattr(
        "styler.component_catalog.executors.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"flatpak", "gdbus"} else None,
    )
    monkeypatch.setattr(
        InitializeFlatpakAppExecutor,
        "_flatpak_state",
        staticmethod(lambda app_id: (False, False, "not running")),
    )

    def repeated_effect(*args, **kwargs):
        raise AssertionError("a completed effect was executed again")

    monkeypatch.setattr(PackageInstallExecutor, "run", repeated_effect)
    monkeypatch.setattr(InitializeFlatpakAppExecutor, "run", repeated_effect)
    monkeypatch.setattr(BackupConfigExecutor, "run", repeated_effect)
    monkeypatch.setattr(OverlayInstallExecutor, "run", repeated_effect)
    monkeypatch.setattr(
        VerifyExecutor,
        "run",
        lambda self, step, ctx: StepResult(
            step.id, step.step_type, True, Status.OK, f"Verificado: {step.id}."
        ),
    )

    result = service.execute("photogimp", "flatpak")

    assert result.ok is True
    assert result.status == ChangeStatus.INTEGRATED
    assert any("no se volverá a instalar" in detail or "reutilizará" in detail for detail in result.details)
    record = service._load_records()["photogimp"]
    assert record["attempt_mode"] == "continue"
    assert record["last_run_id"]
    assert {
        "app.gimp.install",
        "app.gimp.initialize",
        "app.photogimp.backup",
        "app.photogimp.install",
    }.issubset(record["reconciled_steps"])
