from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from styler.automation.conditions import CallableCondition, LatchedCondition, wait_until
from styler.changes.service import ChangeService
from styler.component_catalog import executors as component_executors
from styler.runtime.models import ExecutionContext, StepDefinition


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 5400
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _step(**overrides) -> StepDefinition:
    config = {
        "application_id": "org.gimp.GIMP",
        "config_root": "${HOME}/.var/app/org.gimp.GIMP/config/GIMP",
        "expected_config_schema": "3.0",
        "startup_timeout_seconds": 0.15,
        "poll_interval_seconds": 0.005,
        "window_stable_seconds": 0,
        "shutdown_timeout_seconds": 0.02,
        "config_flush_timeout_seconds": 0.1,
        "config_quiet_seconds": 0,
    }
    config.update(overrides)
    return StepDefinition("gimp-init", "initialize_flatpak_app", config=config)


def test_latched_condition_keeps_a_transient_success() -> None:
    values = iter((True, False, False))
    condition = LatchedCondition(
        CallableCondition("señal fugaz", lambda: next(values)),
        name="hito fugaz observado",
    )

    assert condition.evaluate() is True
    assert condition.evaluate() is True
    assert "latched=True" in condition.diagnostic()


def test_gimp_readiness_accumulates_window_then_config(
    tmp_path: Path, monkeypatch
) -> None:
    process = FakeProcess()
    launched = {"value": False}
    probes = {"count": 0}
    version_probes = {"count": 0}
    version_dir = tmp_path / ".var/app/org.gimp.GIMP/config/GIMP/3.0"

    def popen(*args, **kwargs):
        launched["value"] = True
        return process

    def state(app_id: str):
        if not launched["value"] or process.returncode is not None:
            return False, False, "not-running"
        probes["count"] += 1
        # La ventana solo se observa en el primer sondeo y después pierde foco.
        return True, probes["count"] == 1, f"probe={probes['count']}"

    def detect_version(config_root: Path):
        version_probes["count"] += 1
        # La carpeta aparece en un sondeo posterior, cuando la ventana ya no
        # reporta foco. Sin hitos acumulativos nunca coincidirían.
        if version_probes["count"] >= 2:
            version_dir.mkdir(parents=True, exist_ok=True)
            return version_dir
        return None

    monkeypatch.setattr(component_executors.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(component_executors.PipeCraftRunner, "spawn", lambda self, *args, **kwargs: popen(*args, **kwargs))
    monkeypatch.setattr(
        component_executors.InitializeFlatpakAppExecutor,
        "_flatpak_state",
        staticmethod(state),
    )
    monkeypatch.setattr(component_executors, "_detect_gimp_version_dir", detect_version)
    monkeypatch.setattr(
        component_executors.InitializeFlatpakAppExecutor,
        "_request_graceful_quit",
        staticmethod(lambda app_id, *args: (False, "simulado")),
    )

    result = component_executors.InitializeFlatpakAppExecutor().run(
        _step(),
        ExecutionContext(root=tmp_path, dry_run=False, values={"home": tmp_path}),
    )

    assert result.success is True
    assert result.data["config_version"] == "3.0"
    assert process.terminated is True


def test_flatpak_background_no_counts_as_an_open_window(monkeypatch) -> None:
    monkeypatch.setattr(
        component_executors,
        "_run_probe",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="Application Active Background\norg.gimp.GIMP no no\n",
            stderr="",
        ),
    )

    running, window, detail = component_executors.InitializeFlatpakAppExecutor._flatpak_state(
        "org.gimp.GIMP"
    )

    assert running is True
    assert window is True
    assert "background='no'" in detail


def test_photogimp_default_startup_timeout_is_ninety_seconds(tmp_path: Path) -> None:
    service = ChangeService(tmp_path, home=tmp_path)
    options = service.default_options("photogimp")

    assert options["startup_timeout_seconds"] == 90.0
