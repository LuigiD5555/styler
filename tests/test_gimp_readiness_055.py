from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from styler.component_catalog import executors as component_executors
from styler.runtime.models import ExecutionContext, StepDefinition


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 5500
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


def _step(**overrides) -> StepDefinition:
    config = {
        "application_id": "org.gimp.GIMP",
        "config_root": "${HOME}/.var/app/org.gimp.GIMP/config/GIMP",
        "expected_config_schema": "3.0",
        "startup_timeout_seconds": 0.2,
        "poll_interval_seconds": 0.005,
        "window_stable_seconds": 0,
        "running_stable_fallback_seconds": 0,
        "shutdown_timeout_seconds": 0.05,
        "config_creation_timeout_seconds": 0.1,
        "config_flush_timeout_seconds": 0.1,
        "config_quiet_seconds": 0,
    }
    config.update(overrides)
    return StepDefinition("gimp-init", "initialize_flatpak_app", config=config)


def test_flatpak_state_keeps_running_when_background_column_is_unsupported(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def run(argv, **kwargs):
        calls.append(tuple(argv))
        columns = next((item for item in argv if item.startswith("--columns=")), "")
        if columns == "--columns=application":
            return SimpleNamespace(returncode=0, stdout="Application\norg.gimp.GIMP\n", stderr="")
        if columns == "--columns=application,active,background":
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Unknown column: background",
            )
        return SimpleNamespace(
            returncode=0,
            stdout="Application Active\norg.gimp.GIMP no\n",
            stderr="",
        )

    monkeypatch.setattr(component_executors, "_run_probe", run)

    running, window, detail = component_executors.InitializeFlatpakAppExecutor._flatpak_state(
        "org.gimp.GIMP"
    )

    assert running is True
    assert window is False
    assert "Unknown column: background" in detail
    assert any("application,active" in " ".join(call) for call in calls)


def test_gimp_configuration_may_be_created_only_during_shutdown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    process = FakeProcess()
    launched = {"value": False}
    version_dir = tmp_path / ".var/app/org.gimp.GIMP/config/GIMP/3.0"

    def popen(*args, **kwargs):
        launched["value"] = True
        return process

    def state(app_id: str):
        if launched["value"] and process.returncode is None:
            return True, True, "running with window"
        return False, False, "stopped"

    def graceful_quit(app_id: str, *args):
        # Reproduce el comportamiento observado: el árbol 3.x no existe hasta
        # que GIMP procesa su cierre normal.
        version_dir.mkdir(parents=True, exist_ok=True)
        (version_dir / "sessionrc").write_text("saved-on-exit", encoding="utf-8")
        process.returncode = 0
        return True, "quit enviado"

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
        staticmethod(graceful_quit),
    )

    result = component_executors.InitializeFlatpakAppExecutor().run(
        _step(),
        ExecutionContext(root=tmp_path, dry_run=False, values={"home": tmp_path}),
    )

    assert result.success is True
    assert result.data["config_version"] == "3.0"
    assert result.data["config_creation"]["satisfied"] is True
    assert version_dir.is_dir()
    assert process.returncode == 0
