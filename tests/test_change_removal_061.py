"""Retiro explícito de cambios integrados en Styler 0.6.1."""
from __future__ import annotations

from pathlib import Path

import pytest

from styler.changes.models import ChangeStatus
from styler.changes.service import ChangeService
from styler.cli import build_parser
from styler.receipts import ReceiptJournal, ReceiptKind
from styler.runtime.graph import topological_order


def _service_with_receipts(tmp_path: Path, *, was_present: bool = False) -> ChangeService:
    root = tmp_path / "library"
    home = tmp_path / "home"
    service = ChangeService(root, home)
    journal = ReceiptJournal(root, "photogimp")
    journal.record(
        run_id="apply-1",
        step_id="app.gimp.install",
        step_type="install_package",
        kind=ReceiptKind.PACKAGE_INSTALLED,
        data={
            "manager": "flatpak",
            "package": "org.gimp.GIMP",
            "was_present": was_present,
        },
        clock=lambda: 10.0,
    )
    journal.record(
        run_id="apply-1",
        step_id="app.photogimp.backup",
        step_type="backup_config",
        kind=ReceiptKind.BACKUP_CREATED,
        data={
            "source": str(home / ".config/GIMP/3.2"),
            "backup": str(root / "backups/GIMP-3.2"),
            "existed": True,
        },
        clock=lambda: 20.0,
    )
    journal.record(
        run_id="apply-1",
        step_id="app.photogimp.install",
        step_type="install_overlay",
        kind=ReceiptKind.PATHS_WRITTEN,
        data={
            "created_paths": [str(home / ".local/share/icons/photogimp.png")],
            "created_directories": [str(home / ".local/share/icons")],
            "overwritten": [
                {
                    "path": str(home / ".config/GIMP/3.2/sessionrc"),
                    "backup": str(root / "backups/sessionrc"),
                }
            ],
        },
        clock=lambda: 30.0,
    )
    service._save_record(
        "photogimp",
        {
            "status": ChangeStatus.INTEGRATED,
            "provider_id": "flatpak",
            "provider_label": "Flathub (Flatpak)",
            "automation_level": "automatic",
        },
    )
    return service


def test_removal_plan_explains_exact_effects_and_uninstalls_last(tmp_path: Path):
    service = _service_with_receipts(tmp_path)
    plan = service.build_removal_plan("photogimp")

    assert plan.operation == "remove"
    assert plan.workflow.operation == "undo"
    assert "1 archivo(s) creado(s)" in plan.summary
    assert "1 archivo(s) sustituido(s)" in plan.summary
    assert "org.gimp.GIMP" in plan.summary
    assert any(phase.label == "Retirando archivos del cambio" for phase in plan.phases)
    assert any(phase.label == "Restaurando configuración original" for phase in plan.phases)
    assert any(phase.label == "Desinstalando org.gimp.GIMP" for phase in plan.phases)

    order = topological_order(plan.workflow.steps)
    package = next(step for step in plan.workflow.steps if step.step_type == "uninstall_package")
    assert order[-1] == package.id
    assert set(package.needs) == {
        step.id for step in plan.workflow.steps if step.step_type != "uninstall_package"
    }


def test_removal_plan_keeps_application_that_existed_before(tmp_path: Path):
    service = _service_with_receipts(tmp_path, was_present=True)
    plan = service.build_removal_plan("photogimp")

    assert "Se conservará: org.gimp.GIMP" in plan.summary
    phase = next(phase for phase in plan.phases if "org.gimp.GIMP" in phase.label)
    assert phase.label == "Conservando org.gimp.GIMP"
    assert "ya existía antes" in phase.description


def test_removal_progress_uses_same_phase_ids_as_preview(tmp_path: Path):
    service = _service_with_receipts(tmp_path)
    plan = service.build_removal_plan("photogimp")
    events = []

    service.rollback_change("photogimp", progress=events.append, dry_run=True)

    preview_ids = {phase.phase_id for phase in plan.phases}
    assert events
    assert {event.phase_id for event in events} <= preview_ids


def test_tui_exposes_remove_button_and_executes_through_progress_screen():
    source = (Path(__file__).resolve().parents[1] / "styler/tui/app.py").read_text()

    assert 'id="remove-change"' in source
    assert "build_removal_plan" in source
    assert "rollback_change" in source
    assert 'self.plan.operation == "remove"' in source


def test_cli_uses_remove_as_canonical_command_without_undo_alias():
    parser = build_parser()
    args = parser.parse_args(["change", "remove", "photogimp"])
    assert args.change_command == "remove"

    with pytest.raises(SystemExit):
        parser.parse_args(["change", "undo", "photogimp"])


def test_removal_engine_is_not_hardcoded_to_photogimp(tmp_path: Path):
    root = tmp_path / "library"
    service = ChangeService(root, tmp_path / "home")
    ReceiptJournal(root, "cursor-ocean").record(
        run_id="apply-1",
        step_id="cursor.install",
        step_type="apply_config",
        kind=ReceiptKind.PATHS_WRITTEN,
        data={"created_paths": [str(tmp_path / "home/.icons/Ocean/cursor.theme")]},
    )
    service._save_record(
        "cursor-ocean",
        {"name": "Cursor Ocean", "status": ChangeStatus.INTEGRATED},
    )

    plan = service.build_removal_plan("cursor-ocean")

    assert plan.name == "Cursor Ocean"
    assert plan.operation == "remove"
    assert plan.workflow.metadata["change_id"] == "cursor-ocean"
    assert any(step.step_type == "undo_remove_paths" for step in plan.workflow.steps)
