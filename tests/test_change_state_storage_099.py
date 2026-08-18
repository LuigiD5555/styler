from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("local_execution_backend")

from styler.changes.storage import ChangeStateWriteError, write_json
import errno
from types import SimpleNamespace

from styler.changes.service import ChangeService
from styler.planning.models import Status, StepResult
from styler.target import Target


def _erofs(path):
    return ChangeStateWriteError(path, OSError(errno.EROFS, "Read-only file system"))


def _ubuntu_service(tmp_path):
    service = ChangeService(root=tmp_path / "library", home=tmp_path / "home")
    service._target = Target(family="ubuntu", distro_id="ubuntu", root=str(tmp_path))
    return service


def test_execute_refuses_to_start_when_change_record_storage_is_read_only(tmp_path, monkeypatch):
    service = _ubuntu_service(tmp_path)
    plan = service.build_plan("affinity-linux")
    monkeypatch.setattr(service, "build_plan", lambda *_a, **_k: plan)

    engine_called = False

    def fail_save(*_a, **_k):
        raise _erofs(service._records_path)

    def should_not_run(*_a, **_k):
        nonlocal engine_called
        engine_called = True
        raise AssertionError("el DAG no debe arrancar sin registro escribible")

    monkeypatch.setattr("styler.changes.execution.save_record", fail_save)
    monkeypatch.setattr("styler.changes.execution.workflow_runtime.execute", should_not_run)

    result = service.execute("affinity-linux")

    assert result.ok is False
    assert result.title.startswith("No se inició")
    assert "solo lectura" in result.message
    assert engine_called is False
    assert any("change-records.json" in line for line in result.details)


def test_post_run_record_failure_does_not_claim_the_dag_itself_failed(tmp_path, monkeypatch):
    service = _ubuntu_service(tmp_path)
    plan = service.build_plan("affinity-linux")
    monkeypatch.setattr(service, "build_plan", lambda *_a, **_k: plan)
    monkeypatch.setattr(service, "plan_requires_admin", lambda _plan: False)

    calls = 0

    def save_then_erofs(*_a, **_k):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise _erofs(service._records_path)

    monkeypatch.setattr("styler.changes.execution.save_record", save_then_erofs)
    monkeypatch.setattr(
        "styler.changes.execution.workflow_runtime.execute",
        lambda *_a, **_k: SimpleNamespace(
            results=(StepResult("node", "verify", True, Status.OK, "DAG terminado"),),
            report_path=str(tmp_path / "report.json"),
            run_id="run-099",
        ),
    )

    result = service.execute("affinity-linux")

    assert result.ok is False  # detiene un lote: no seguir sin estado persistente
    assert result.status == "needs_attention"
    assert "perdió acceso a su estado" in result.title
    assert "DAG pudo haber producido efectos" in "\n".join(result.details)
    assert "No se pudo completar Affinity" not in result.title
    assert result.diagnostic_path


def test_atomic_json_cleans_temporary_file_after_write_failure(tmp_path, monkeypatch):
    target = tmp_path / "change-records.json"
    temporary = tmp_path / "change-records.json.tmp"

    original_replace = __import__("os").replace

    def fail_replace(*_a, **_k):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr("styler.changes.storage.os.replace", fail_replace)
    try:
        try:
            write_json(target, {"x": 1})
        except OSError as exc:
            assert exc.errno == errno.EROFS
        else:
            raise AssertionError("se esperaba un error de escritura")
    finally:
        monkeypatch.setattr("styler.changes.storage.os.replace", original_replace)

    assert not temporary.exists()
