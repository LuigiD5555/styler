from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("local_execution_backend")

import errno
from pathlib import Path

from styler.changes import ChangeService
from styler.changes.storage import ChangeStateWriteError, probe_directory_writable
from styler.target import Target


def _service(tmp_path: Path) -> ChangeService:
    service = ChangeService(tmp_path / "library", tmp_path / "home")
    service._target = Target(
        family="ubuntu",
        distro_id="linuxmint",
        pretty_name="Linux Mint 22.3",
        root=str(tmp_path),
    )
    return service


def test_runtime_erofs_after_progress_becomes_failed_current_change_not_unstarted_batch(tmp_path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(service, "plan_requires_admin", lambda _plan: False)

    def erofs_after_progress(_workflow, context, _registry=None):
        callback = context.values["progress_callback"]
        callback({
            "step_id": "appimagelauncher-download",
            "phase_label": "Descargando",
            "operation": "trabajando",
            "message": "trabajando",
            "total_progress": 0.4,
            "status": "running",
            "event_type": "command_output",
            "terminal_line": "40%",
        })
        path = service.root / ".styler" / "runs" / "demo" / "state.json.tmp"
        raise OSError(errno.EROFS, "Read-only file system", str(path))

    monkeypatch.setattr("styler.changes.execution.workflow_runtime.execute", erofs_after_progress)

    result = service.execute_batch(["appimagelauncher", "affinity-linux"])

    assert result.ok is False
    assert len(result.results) == 1
    failed = result.results[0]
    assert failed.change_id == "appimagelauncher"
    assert failed.status == "needs_attention"
    assert "solo lectura" in failed.message
    assert "perdió acceso" in failed.title
    assert result.skipped_ids == ("affinity-linux",)
    assert "No se pudo iniciar el lote" not in result.title


def test_preflight_checks_pipecraft_runs_before_starting_dag(tmp_path, monkeypatch):
    service = _service(tmp_path)
    engine_called = False

    original_probe = probe_directory_writable

    def fail_runs(path: Path):
        if path == service.root / ".styler" / "runs":
            raise ChangeStateWriteError(
                path, OSError(errno.EROFS, "Read-only file system", str(path))
            )
        return original_probe(path)

    def should_not_run(*_a, **_k):
        nonlocal engine_called
        engine_called = True
        raise AssertionError("PipeCraft no debe arrancar con runs/ en solo lectura")

    monkeypatch.setattr("styler.changes.service.probe_directory_writable", fail_runs)
    monkeypatch.setattr("styler.changes.execution.workflow_runtime.execute", should_not_run)

    result = service.execute("appimagelauncher")

    assert result.ok is False
    assert result.title.startswith("No se inició")
    assert "solo lectura" in result.message
    assert engine_called is False
    assert any(".styler/runs" in line for line in result.details)


def test_rollback_refuses_before_effects_when_state_storage_is_read_only(tmp_path, monkeypatch):
    from styler.receipts import ReceiptKind

    root = tmp_path / "library"
    home = tmp_path / "home"
    home.mkdir()
    service = ChangeService(root=root, home=home)

    target = home / ".config/styler-demo/demo.txt"
    target.parent.mkdir(parents=True)
    target.write_text("demo\n", encoding="utf-8")
    service.journal_for_change("photogimp").record(
        run_id="apply-demo",
        step_id="demo.write",
        step_type="write_paths",
        kind=ReceiptKind.PATHS_WRITTEN,
        data={"created_paths": [str(target)], "overwritten": []},
    )
    assert service.can_rollback("photogimp") is True

    def readonly(_change_id: str):
        path = root / ".styler" / "runs"
        raise ChangeStateWriteError(
            path, OSError(errno.EROFS, "Read-only file system", str(path))
        )

    monkeypatch.setattr(service, "_assert_execution_storage_writable", readonly)

    result = service.rollback_change("photogimp")

    assert result.ok is False
    assert result.title.startswith("No se inició el retiro")
    assert "solo lectura" in result.message
    assert target.exists()
