from __future__ import annotations

import time
from pathlib import Path

from styler.changes.service import ChangeService
from styler.component_catalog.executors import CreateChangeCheckpointExecutor, extended_registry
from styler.receipts import ReceiptJournal, ReceiptKind
from styler.runtime.engine import WorkflowEngine
from styler.runtime.commands import CommandResult
from styler.runtime.executors import PackageInstallExecutor
import styler.runtime.executors as runtime_executors
from styler.runtime.models import ExecutionContext, Status, StepDefinition, WorkflowDefinition


def _create_checkpoint(tmp_path: Path, home: Path, change_id: str, run_id: str) -> None:
    config_dir = home / f".config/{change_id}"
    config_dir.mkdir(parents=True)
    (config_dir / "file").write_text("original")
    step = StepDefinition(
        "change.checkpoint",
        "create_change_checkpoint",
        config={"checkpoint_id": f"{change_id}-initial", "paths": [str(config_dir)]},
    )
    ctx = ExecutionContext(
        root=tmp_path,
        dry_run=False,
        values={"home": str(home), "change_id": change_id},
    ).for_run(run_id)
    result = CreateChangeCheckpointExecutor().run(step, ctx)
    assert result.success
    time.sleep(0.001)


def test_change_plan_starts_with_reversible_checkpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(PackageInstallExecutor, "_is_installed", staticmethod(lambda manager, name: False))
    service = ChangeService(tmp_path / "library", tmp_path / "home")

    plan = service.build_plan("photogimp", "flatpak")

    assert plan.workflow.steps[0].id == "change.checkpoint"
    assert plan.workflow.steps[0].step_type == "create_change_checkpoint"
    effectful = {
        "install_package",
        "initialize_flatpak_app",
        "backup_config",
        "install_overlay",
    }
    for step in plan.workflow.steps[1:]:
        if step.step_type in effectful:
            assert "change.checkpoint" in step.needs


def test_checkpoint_records_existing_and_missing_paths(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    existing = home / ".config/GIMP"
    existing.mkdir(parents=True)
    (existing / "sessionrc").write_text("original")
    missing = home / ".var/app/org.gimp.GIMP/config/GIMP"
    monkeypatch.setattr(PackageInstallExecutor, "_is_installed", staticmethod(lambda manager, name: False))
    step = StepDefinition(
        "change.checkpoint",
        "create_change_checkpoint",
        config={
            "checkpoint_id": "photogimp-initial",
            "paths": [str(existing), str(missing)],
            "packages": [{"manager": "flatpak", "name": "org.gimp.GIMP"}],
        },
    )
    ctx = ExecutionContext(
        root=tmp_path,
        dry_run=False,
        values={"home": str(home), "change_id": "photogimp"},
    ).for_run("run-1")

    result = CreateChangeCheckpointExecutor().run(step, ctx)

    assert result.success
    assert result.data["checkpoint_id"] == "photogimp-initial-run-1"
    assert result.data["paths"][0]["existed"] is True
    assert Path(result.data["paths"][0]["backup"], "sessionrc").read_text() == "original"
    assert result.data["paths"][1]["existed"] is False
    assert result.data["packages"][0]["was_present"] is False
    assert ReceiptJournal(tmp_path, "photogimp").pending_undo()[0].kind == ReceiptKind.CHECKPOINT_CREATED


def test_checkpoint_undo_restores_initial_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / ".var/app/org.gimp.GIMP/config/GIMP"
    config.mkdir(parents=True)
    (config / "sessionrc").write_text("before")
    checkpoint = StepDefinition(
        "change.checkpoint",
        "create_change_checkpoint",
        config={"checkpoint_id": "photogimp-initial", "paths": [str(config)]},
    )
    ctx = ExecutionContext(
        root=tmp_path,
        dry_run=False,
        values={"home": str(home), "change_id": "photogimp"},
    ).for_run("run-1")
    assert CreateChangeCheckpointExecutor().run(checkpoint, ctx).success
    (config / "sessionrc").write_text("after")
    (config / "new-file").write_text("created")

    service = ChangeService(tmp_path, home)
    result = service.rollback_change("photogimp")

    assert result.ok
    assert (config / "sessionrc").read_text() == "before"
    assert not (config / "new-file").exists()


def test_checkpoint_undo_removes_path_absent_before_apply(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / ".var/app/org.gimp.GIMP/config/GIMP"
    checkpoint = StepDefinition(
        "change.checkpoint",
        "create_change_checkpoint",
        config={"checkpoint_id": "photogimp-initial", "paths": [str(config)]},
    )
    ctx = ExecutionContext(
        root=tmp_path,
        dry_run=False,
        values={"home": str(home), "change_id": "photogimp"},
    ).for_run("run-1")
    assert CreateChangeCheckpointExecutor().run(checkpoint, ctx).success
    config.mkdir(parents=True)
    (config / "created").write_text("new")

    result = ChangeService(tmp_path, home).rollback_change("photogimp")

    assert result.ok
    assert not config.exists()


def test_failed_package_install_registers_reversible_partial_install(monkeypatch, tmp_path: Path) -> None:
    # El paquete no está presente antes de empezar y el gestor lo deja
    # instalado a medias antes de devolver un código de error. Se modela el
    # estado real en vez de un número fijo de consultas: la cantidad de sondas
    # es un detalle del motor y no debe formar parte del contrato.
    present = {"value": False}

    def install_left_it_present(ctx, step, argv, **kwargs):
        present["value"] = True
        return CommandResult(1, "", "failed", command=tuple(argv), log_path=str(tmp_path / "install.log"))

    monkeypatch.setattr(
        PackageInstallExecutor, "_is_installed",
        staticmethod(lambda manager, name: present["value"]),
    )
    monkeypatch.setattr(PackageInstallExecutor, "_install_argv", classmethod(lambda cls, manager, name: ["flatpak", "install", name]))
    monkeypatch.setattr(PackageInstallExecutor, "_ensure_flathub", staticmethod(lambda ctx, step: None))
    monkeypatch.setattr(runtime_executors, "run_step_command", install_left_it_present)

    step = StepDefinition(
        "app.gimp.install",
        "install_package",
        config={"package": {"manager": "flatpak", "name": "org.gimp.GIMP"}},
    )
    workflow = WorkflowDefinition("install", [step])
    registry = extended_registry()
    run = WorkflowEngine(registry).run(
        workflow,
        ExecutionContext(root=tmp_path, dry_run=False, values={"change_id": "photogimp"}),
    )

    assert not run.success
    receipts = ReceiptJournal(tmp_path, "photogimp").pending_undo()
    assert receipts[0].kind == ReceiptKind.PACKAGE_INSTALLED
    undo = ChangeService(tmp_path, tmp_path / "home").rollback_plan("photogimp")
    assert undo.steps[0].step_type == "uninstall_package"


def test_creating_a_checkpoint_prunes_automatically_past_five(tmp_path: Path) -> None:
    """CreateChangeCheckpointExecutor poda tras cada creación (SYSTEM_CHECKPOINT_LIMIT)."""
    home = tmp_path / "home"
    home.mkdir()
    service = ChangeService(tmp_path, home)
    for index in range(7):
        _create_checkpoint(tmp_path, home, f"change-{index}", f"run-{index}")

    assert len(service._all_checkpoint_receipts()) == 5
    assert len(service.system_checkpoints()) == 5
    checkpoints_root = tmp_path / ".styler" / "checkpoints"
    remaining = {path.name for path in checkpoints_root.iterdir()}
    assert "change-0-initial-run-0" not in remaining
    assert "change-1-initial-run-1" not in remaining
    assert "change-6-initial-run-6" in remaining


def test_prune_preserves_checkpoints_backing_live_receipts(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    service = ChangeService(tmp_path, home)
    _create_checkpoint(tmp_path, home, "change-0", "run-0")

    journal = ReceiptJournal(tmp_path, "change-0")
    journal.record(
        run_id="run-0",
        step_id="app.install",
        step_type="install_package",
        kind=ReceiptKind.PACKAGE_INSTALLED,
        data={"manager": "flatpak", "package": "org.example.App", "was_present": False},
    )

    for index in range(1, 6):
        _create_checkpoint(tmp_path, home, f"change-{index}", f"run-{index}")

    pruned = service.prune_system_checkpoints(keep=5)

    assert pruned == ()
    live_checkpoint = [
        item
        for item in ReceiptJournal(tmp_path, "change-0").pending_undo()
        if item.kind == ReceiptKind.CHECKPOINT_CREATED
    ]
    assert live_checkpoint
    checkpoints_root = tmp_path / ".styler" / "checkpoints"
    assert (checkpoints_root / "change-0-initial-run-0").is_dir()
