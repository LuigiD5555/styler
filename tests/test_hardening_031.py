"""Regresiones de seguridad y honestidad incorporadas en 0.3.1."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from styler.automation.conditions import ConditionState, WaitResult, evaluate_state
from styler.automation.diagnostics import capture_wait_failure
from styler.changes.models import ChangeOption, ChangeStatus
from styler.changes.service import ChangeService
from styler.component_catalog.executors import BackupConfigExecutor
from styler.receipts import ReceiptJournal, ReceiptKind, ReceiptWriteError, emit_receipt
from styler.runtime.models import ExecutionContext, StepDefinition


def test_boolean_options_parse_false_explicitly():
    option = ChangeOption("backup", "Backup", "")
    assert option.coerce("false") is False
    assert option.coerce("0") is False
    assert option.coerce("no") is False
    assert option.coerce("true") is True
    with pytest.raises(ValueError):
        option.coerce("quizá")


def test_diagnostic_uses_last_snapshot_without_evaluating_again(tmp_path):
    class CountingCondition:
        name = "cuenta"
        def __init__(self):
            self.calls = 0
        def state(self):
            self.calls += 1
            return ConditionState.PENDING
        def diagnostic(self):
            return f"calls={self.calls}"

    condition = CountingCondition()
    assert evaluate_state(condition) is ConditionState.PENDING
    result = WaitResult(False, condition.name, 1.0, 1, "calls=1", "timeout")
    capture_wait_failure(result, root=tmp_path, scope="test", condition=condition)
    assert condition.calls == 1


def test_receipt_failure_is_not_silenced(monkeypatch, tmp_path):
    ctx = ExecutionContext(
        root=tmp_path,
        dry_run=False,
        values={"change_id": "photogimp"},
    )
    step = StepDefinition(id="s", step_type="install_overlay")

    def fail(self, **kwargs):
        raise OSError("disco lleno")

    monkeypatch.setattr(ReceiptJournal, "record", fail)
    with pytest.raises(ReceiptWriteError):
        emit_receipt(ctx, step, ReceiptKind.PATHS_WRITTEN, {"created_paths": []})
    assert ctx.values["receipt_errors"]


def test_effect_does_not_start_when_journal_is_unavailable(monkeypatch, tmp_path):
    home = tmp_path / "home"
    source = home / ".config" / "GIMP"
    source.mkdir(parents=True)
    (source / "gimprc").write_text("original")
    ctx = ExecutionContext(
        root=tmp_path,
        dry_run=False,
        values={"home": str(home), "change_id": "photogimp"},
    )

    def fail(self):
        raise OSError("solo lectura")

    monkeypatch.setattr(ReceiptJournal, "ensure_writable", fail)
    result = BackupConfigExecutor().run(
        StepDefinition(
            id="backup", step_type="backup_config",
            config={"backup_source": "${HOME}/.config/GIMP"},
        ),
        ctx,
    )
    assert not result.success
    assert result.data["error_code"] == "RECEIPT_JOURNAL_UNAVAILABLE"
    assert not (tmp_path / ".styler" / "component-backups").exists()


def test_package_receipt_is_consumed_when_package_is_already_absent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    from styler.runtime.undo_executors import PackageUninstallExecutor
    monkeypatch.setattr(
        PackageUninstallExecutor,
        "_probe_installed",
        staticmethod(lambda manager, package: False),
    )
    service = ChangeService(root=tmp_path / "library", home=home)
    service._save_record(
        "photogimp",
        {
            "status": ChangeStatus.INTEGRATED,
            "provider_id": "flatpak",
            "provider_label": "Flathub (Flatpak)",
            "automation_level": "automatic",
        },
    )
    journal = service.journal_for_change("photogimp")
    receipt = journal.record(
        run_id="r1",
        step_id="app.gimp.install",
        step_type="install_package",
        kind=ReceiptKind.PACKAGE_INSTALLED,
        data={"manager": "flatpak", "package": "org.gimp.GIMP", "was_present": False},
    )
    result = service.rollback_change("photogimp")
    assert result.ok
    assert result.status == ChangeStatus.REVERTED
    assert receipt not in journal.pending_undo()
