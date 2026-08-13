from __future__ import annotations

from pathlib import Path

from styler.component_catalog import executors as component_executors
from styler.runtime.models import ExecutionContext, Status, StepDefinition


class FakeProcess:
    def __init__(self):
        self.pid = 4242
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def make_step(**overrides):
    config = {
        "application_id": "org.gimp.GIMP",
        "config_root": "${HOME}/.var/app/org.gimp.GIMP/config/GIMP",
        "expected_config_schema": "3.0",
        "startup_timeout_seconds": 0.05,
        "poll_interval_seconds": 0.005,
        "window_stable_seconds": 0,
        "shutdown_timeout_seconds": 0.02,
        "config_flush_timeout_seconds": 0.1,
        "config_quiet_seconds": 0,
    }
    config.update(overrides)
    return StepDefinition("gimp-init", "initialize_flatpak_app", config=config)


def prepare_flatpak(monkeypatch, process: FakeProcess, *, active: bool = True):
    launched = {"value": False}

    def popen(*args, **kwargs):
        launched["value"] = True
        return process

    def state(app_id: str):
        if not launched["value"] or process.returncode is not None:
            return False, False, "not-running"
        return True, active, "org.gimp.GIMP yes" if active else "org.gimp.GIMP no"

    monkeypatch.setattr(component_executors.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(component_executors.PipeCraftRunner, "spawn", lambda self, *args, **kwargs: popen(*args, **kwargs))
    monkeypatch.setattr(
        component_executors.InitializeFlatpakAppExecutor,
        "_flatpak_state",
        staticmethod(state),
    )
    monkeypatch.setattr(
        component_executors.InitializeFlatpakAppExecutor,
        "_request_graceful_quit",
        staticmethod(lambda app_id, *args: (False, "sin interfaz GTK simulada")),
    )
    return launched


def test_photogimp_initialization_waits_for_active_window_and_real_config(tmp_path: Path, monkeypatch):
    process = FakeProcess()
    prepare_flatpak(monkeypatch, process)
    version_dir = tmp_path / ".var/app/org.gimp.GIMP/config/GIMP/3.2"
    version_dir.mkdir(parents=True)

    result = component_executors.InitializeFlatpakAppExecutor().run(
        make_step(),
        ExecutionContext(root=tmp_path, dry_run=False, values={"home": tmp_path}),
    )

    assert result.success is True
    assert result.data["config_version"] == "3.2"
    assert result.data["readiness"]["reason"] == "satisfied"
    assert result.data["lifecycle_state"] == "stopped"
    assert process.terminated is True


def test_photogimp_initialization_accepts_preexisting_config_without_active_window(
    tmp_path: Path, monkeypatch
):
    process = FakeProcess()
    prepare_flatpak(monkeypatch, process, active=False)
    version_dir = tmp_path / ".var/app/org.gimp.GIMP/config/GIMP/3.2"
    version_dir.mkdir(parents=True)

    result = component_executors.InitializeFlatpakAppExecutor().run(
        make_step(startup_timeout_seconds=0.02, poll_interval_seconds=0.005),
        ExecutionContext(root=tmp_path, dry_run=False, values={"home": tmp_path}),
    )

    assert result.success is True
    assert result.data["config_version"] == "3.2"
    assert result.data["readiness"]["reason"] == "satisfied"
    assert process.terminated is True


def test_photogimp_initialization_still_requires_window_for_new_config(
    tmp_path: Path, monkeypatch
):
    process = FakeProcess()
    prepare_flatpak(monkeypatch, process, active=False)
    version_dir = tmp_path / ".var/app/org.gimp.GIMP/config/GIMP/3.2"
    probes = {"count": 0}

    def detect_version(config_root: Path):
        probes["count"] += 1
        if probes["count"] == 1:
            return None
        version_dir.mkdir(parents=True, exist_ok=True)
        return version_dir

    monkeypatch.setattr(component_executors, "_detect_gimp_version_dir", detect_version)

    result = component_executors.InitializeFlatpakAppExecutor().run(
        make_step(startup_timeout_seconds=0.02, poll_interval_seconds=0.005),
        ExecutionContext(root=tmp_path, dry_run=False, values={"home": tmp_path}),
    )

    assert result.success is False
    assert result.status == Status.TIMEOUT
    assert result.data["error_code"] == "APP_READY_TIMEOUT"
    assert process.terminated is True


def test_photogimp_waits_for_configuration_tree_after_shutdown(tmp_path: Path, monkeypatch):
    process = FakeProcess()
    prepare_flatpak(monkeypatch, process)
    version_dir = tmp_path / ".var/app/org.gimp.GIMP/config/GIMP/3.2"
    version_dir.mkdir(parents=True)

    def graceful_quit(app_id: str, *args):
        # Simula el archivo que GIMP termina de escribir durante File > Quit.
        (version_dir / "sessionrc").write_text("saved-on-exit")
        process.returncode = 0
        return True, "quit enviado"

    monkeypatch.setattr(
        component_executors.InitializeFlatpakAppExecutor,
        "_request_graceful_quit",
        staticmethod(graceful_quit),
    )

    result = component_executors.InitializeFlatpakAppExecutor().run(
        make_step(
            config_quiet_seconds=0.03,
            config_flush_timeout_seconds=0.2,
        ),
        ExecutionContext(root=tmp_path, dry_run=False, values={"home": tmp_path}),
    )

    assert result.success is True
    assert result.data["graceful_quit"] is True
    assert result.data["config_flush"]["elapsed_seconds"] >= 0.02
    assert (version_dir / "sessionrc").read_text() == "saved-on-exit"


def test_photogimp_rejects_an_already_running_gimp(tmp_path: Path, monkeypatch):
    process = FakeProcess()
    monkeypatch.setattr(component_executors.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        component_executors.InitializeFlatpakAppExecutor,
        "_flatpak_state",
        staticmethod(lambda app_id: (True, True, "org.gimp.GIMP yes")),
    )

    result = component_executors.InitializeFlatpakAppExecutor().run(
        make_step(),
        ExecutionContext(root=tmp_path, dry_run=False, values={"home": tmp_path}),
    )

    assert result.success is False
    assert result.data["error_code"] == "APP_ALREADY_RUNNING"
