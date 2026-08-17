from __future__ import annotations

from pathlib import Path

from styler.changes import (
    AutomationLevel,
    ChangeExecutionResult,
    ChangeProgressEvent,
    ChangeService,
    ChangeStatus,
)
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


def _result(change_id: str, name: str, *, ok: bool) -> ChangeExecutionResult:
    return ChangeExecutionResult(
        change_id=change_id,
        name=name,
        ok=ok,
        status=ChangeStatus.INTEGRATED if ok else ChangeStatus.FAILED,
        title=f"{name} {'listo' if ok else 'falló'}",
        message="ok" if ok else "fallo controlado",
        provider_id="yaml",
        provider_label="YAML",
        automation_level=AutomationLevel.AUTOMATIC,
    )


def test_batch_orders_selected_yaml_dependency_before_consumer(tmp_path: Path):
    service = _service(tmp_path)
    batch = service.build_batch_plan(["affinity-linux", "appimagelauncher"])

    assert batch.change_ids == ("appimagelauncher", "affinity-linux")
    assert [plan.name for plan in batch.plans] == ["AppImageLauncher", "Affinity para Linux"]


def test_batch_deduplicates_same_selected_change(tmp_path: Path):
    service = _service(tmp_path)
    batch = service.build_batch_plan(
        ["appimagelauncher", "appimagelauncher", "affinity-linux"]
    )
    assert batch.change_ids == ("appimagelauncher", "affinity-linux")


def test_batch_preview_does_not_repeat_dependency_already_scheduled(tmp_path: Path):
    service = _service(tmp_path)
    batch = service.build_batch_plan(["affinity-linux", "appimagelauncher"])
    affinity = batch.plans[1]

    assert not any(
        phase.step_id.startswith("yaml.appimagelauncher.")
        for phase in affinity.phases
    )
    assert "Dependencia ya programada antes" in affinity.notice
    # El workflow real no se mutila: se reconciliará al ejecutarlo.
    assert any(
        step.id.startswith("yaml.appimagelauncher.")
        for step in affinity.workflow.steps
    )


def test_batch_executes_sequentially_and_stops_before_next_change(tmp_path: Path, monkeypatch):
    service = _service(tmp_path)
    calls: list[str] = []
    progress_events = []

    def fake_execute(change_id, provider_id=None, progress=None, options=None):
        calls.append(change_id)
        names = {
            "appimagelauncher": "AppImageLauncher",
            "affinity-linux": "Affinity para Linux",
            "photogimp": "PhotoGIMP",
        }
        if progress is not None:
            progress(
                ChangeProgressEvent(
                    change_id=change_id,
                    change_name=names[change_id],
                    phase_id="test",
                    phase_label="Prueba",
                    operation="Ejecutando",
                    phase_index=1,
                    phase_count=1,
                    phase_progress=1.0,
                    total_progress=1.0,
                    status="completed" if change_id != "affinity-linux" else "failed",
                )
            )
        return _result(change_id, names[change_id], ok=change_id != "affinity-linux")

    monkeypatch.setattr(service, "execute", fake_execute)
    result = service.execute_batch(
        ["appimagelauncher", "affinity-linux", "photogimp"],
        progress_events.append,
    )

    assert calls == ["appimagelauncher", "affinity-linux"]
    assert result.ok is False
    assert result.skipped_ids == ("photogimp",)
    assert result.skipped_names == ("PhotoGIMP",)
    assert result.failed_result is not None
    assert result.failed_result.change_id == "affinity-linux"
    assert progress_events
    assert all(0.0 <= event.total_progress <= 1.0 for event in progress_events)


def test_batch_ui_uses_clickable_rows_and_one_contextual_integration_action():
    source = Path("styler/tui/app.py").read_text(encoding="utf-8")
    assert 'id=f"batch-select-{safe}"' not in source
    assert 'id="integrate-batch"' not in source
    assert source.count('id="integrate-change"') == 1
    assert 'f"Integrar lote ({count})"' in source
    assert '"1 cambio seleccionado · integración individual."' in source
    assert "ChangeBatchReviewScreen" in source
    assert "ChangeBatchProgressScreen" in source
    assert "ChangeBatchResultScreen" in source
    assert "execute_batch" in source


def test_existing_single_change_progress_screen_remains_byte_identical():
    import hashlib

    source = Path("styler/tui/app.py").read_text(encoding="utf-8")
    start = source.index("class ChangeProgressScreen(Screen):")
    end = source.index("class ChangeResultScreen(Screen):")
    digest = hashlib.sha256(source[start:end].encode("utf-8")).hexdigest()
    assert digest == "c684a0dc239d47d50c638efda18c528f72fdedae0318400b4e544bec6be45732"
