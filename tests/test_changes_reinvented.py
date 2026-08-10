from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

from styler.changes import AutomationLevel, ChangeService, ChangeStatus
from styler.component_catalog.executors import ManualHandoffExecutor, extended_registry
from styler.component_catalog.loader import load
from styler.component_catalog.registry import ComponentRegistry
from styler.component_catalog.resolver import resolve
from styler.runtime.engine import WorkflowEngine
from styler.runtime.models import ExecutionContext, StepDefinition, WorkflowDefinition
from styler.target import Target


def test_photogimp_usa_flatpak_por_default_pero_respeta_apt_explicito():
    registry = ComponentRegistry.from_report(load(root="."))
    default = resolve(registry, ["app.photogimp"], family="ubuntu")
    explicit = resolve(
        registry,
        ["app.photogimp"],
        family="ubuntu",
        preferred_providers={"app.gimp": "apt"},
    )
    assert default.selected_providers["app.gimp"] == "flatpak"
    assert explicit.selected_providers["app.gimp"] == "apt"


def test_plan_automatico_y_asistido_son_ramas_del_mismo_cambio(tmp_path: Path):
    service = ChangeService(tmp_path / "library", tmp_path / "home")
    service._target = Target(family="ubuntu", distro_id="ubuntu", root=str(tmp_path))

    automatic = service.build_plan("photogimp", "flatpak")
    assisted = service.build_plan("photogimp", "apt")

    assert automatic.change_id == assisted.change_id == "photogimp"
    assert automatic.automation_level == AutomationLevel.AUTOMATIC
    assert assisted.automation_level == AutomationLevel.ASSISTED
    assert [phase.step_id for phase in automatic.phases] == [
        "change.checkpoint",
        "app.gimp.install",
        "app.gimp.resolve-facts",
        "app.gimp.initialize",
        "app.gimp.verify",
        "app.photogimp.backup",
        "app.photogimp.install",
        "app.photogimp.verify",
        # El instructivo oficial termina abriendo GIMP: verificar los archivos
        # no demuestra que arranque con ellos.
        "app.photogimp.launch",
    ]
    assert [phase.step_id for phase in assisted.phases] == [
        "change.checkpoint",
        "app.gimp.install",
        "app.gimp.verify",
        "app.photogimp.handoff",
    ]
    assert abs(sum(phase.weight for phase in automatic.phases) - 1.0) < 1e-9
    assert abs(sum(phase.weight for phase in assisted.phases) - 1.0) < 1e-9


def test_arch_expone_pacman_y_aur_sin_cambiar_la_identidad_del_cambio(tmp_path: Path):
    service = ChangeService(tmp_path / "library", tmp_path / "home")
    service._target = Target(family="arch", distro_id="arch", root=str(tmp_path))
    options = {option.provider_id: option for option in service.provider_options("photogimp")}
    assert {"flatpak", "pacman", "aur", "snap"}.issubset(options)
    assert options["flatpak"].recommended is True
    assert options["aur"].automation_level == AutomationLevel.ASSISTED


def test_detecta_photogimp_como_un_cambio_y_no_como_rutas_sueltas(tmp_path: Path):
    home = tmp_path / "home"
    marker = home / ".config/GIMP/.photogimp-marker"
    marker.parent.mkdir(parents=True)
    marker.write_text("source=test\nprovider=flatpak\n", encoding="utf-8")
    service = ChangeService(tmp_path / "library", home)

    integrated = service.integrated_changes()
    assert len(integrated) == 1
    assert integrated[0].change_id == "photogimp"
    assert integrated[0].status == ChangeStatus.INTEGRATED
    assert integrated[0].provider_id == "flatpak"


def test_handoff_manual_descarga_en_downloads_y_no_toca_config(monkeypatch, tmp_path: Path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as bundle:
        bundle.writestr(".config/GIMP/3.0/gimprc", "photogimp")
    archive_bytes = payload.getvalue()

    class Response(io.BytesIO):
        headers = {"Content-Length": str(len(archive_bytes))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(
        "styler.component_catalog.executors.urlopen",
        lambda request, timeout=45: Response(archive_bytes),
    )
    monkeypatch.setattr(
        "styler.component_catalog.executors.shutil.which",
        lambda name: None,
    )

    home = tmp_path / "home"
    ctx = ExecutionContext(
        root=tmp_path / "library",
        dry_run=False,
        values={"home": str(home)},
        run_dir=tmp_path / "run",
        artifacts_dir=tmp_path / "run/artifacts",
        logs_dir=tmp_path / "run/logs",
    )
    step = StepDefinition(
        id="app.photogimp.handoff",
        step_type="prepare_manual_handoff",
        config={
            "source": "https://github.com/Diolinux/PhotoGIMP/releases/download/3.0/PhotoGIMP-linux.zip",
            "change_name": "PhotoGIMP",
            "provider_label": "APT",
        },
    )
    result = ManualHandoffExecutor().run(step, ctx)
    assert result.success
    assert Path(result.data["handoff_path"]).is_file()
    assert Path(result.data["instructions_path"]).is_file()
    assert not (home / ".config").exists()
    assert not (home / ".local").exists()


def test_motor_publica_porcentaje_total_y_fase_actual(tmp_path: Path):
    events: list[dict] = []
    workflow = WorkflowDefinition(
        name="progress-contract",
        steps=[
            StepDefinition(id="one", step_type="note", description="Primera", config={"message": "uno"}),
            StepDefinition(id="two", step_type="note", description="Segunda", needs=["one"], config={"message": "dos"}),
        ],
        metadata={
            "max_workers": 1,
            "progress_weights": {"one": 0.25, "two": 0.75},
            "progress_labels": {"one": "Primera fase", "two": "Segunda fase"},
        },
    )
    ctx = ExecutionContext(
        root=tmp_path,
        dry_run=False,
        approve=True,
        values={"progress_callback": events.append},
    )
    run = WorkflowEngine(extended_registry()).run(workflow, ctx)
    assert run.success
    assert events
    assert events[-1]["total_progress"] == 1.0
    assert any(event["phase_label"] == "Primera fase" for event in events)
    assert any(event["phase_label"] == "Segunda fase" for event in events)


def test_plan_photogimp_separa_inicializacion_flatpak_del_overlay_xdg(tmp_path: Path):
    service = ChangeService(tmp_path / "library", tmp_path / "home")
    service._target = Target(family="ubuntu", distro_id="ubuntu", root=str(tmp_path))
    workflow = service._build_automatic_photogimp("flatpak")
    by_id = {step.id: step for step in workflow.steps}

    assert by_id["app.gimp.initialize"].config["config_root"] == "${HOME}/.var/app/org.gimp.GIMP/config/GIMP"
    assert by_id["app.photogimp.backup"].config["backup_source"] == "${HOME}/.config/GIMP"
    assert by_id["app.photogimp.install"].config["target"] == "${HOME}/.config/GIMP"
    assert by_id["app.photogimp.verify"].config["target"] == "${HOME}/.config/GIMP"
    assert by_id["app.photogimp.install"].config["initialization_path_independent"] is True
