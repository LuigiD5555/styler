from __future__ import annotations

import json
import sys
from pathlib import Path

from styler.runtime.commands import PipeCraftRunner, run_step_command
from styler.runtime.engine import WorkflowEngine
from styler.runtime.executors import ExecutorRegistry, StepExecutor
from styler.runtime.models import (
    ExecutionContext,
    Status,
    StepDefinition,
    StepResult,
    WorkflowDefinition,
)


class ObservableCommandExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "observable_command"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        result = run_step_command(
            ctx,
            step,
            [
                sys.executable,
                "-c",
                (
                    "import time; "
                    "print('linea-uno', flush=True); "
                    "time.sleep(0.35); "
                    "print('linea-dos', flush=True)"
                ),
            ],
            timeout=5,
            label="Comando observable de prueba",
        )
        return StepResult(
            step.id,
            step.step_type,
            result.returncode == 0,
            Status.OK if result.returncode == 0 else Status.FAILED,
            "comando terminado",
            output=result.stdout,
            data={"log_path": result.log_path},
        )


def test_pipecraft_streams_output_heartbeats_and_persists_events(tmp_path: Path) -> None:
    observed: list[dict] = []
    registry = ExecutorRegistry()
    registry.register(ObservableCommandExecutor())
    workflow = WorkflowDefinition(
        "observable",
        [StepDefinition("external", "observable_command")],
    )
    context = ExecutionContext(
        root=tmp_path,
        values={
            "progress_callback": observed.append,
            "command_runner": PipeCraftRunner(heartbeat_interval=0.05),
        },
    )

    run = WorkflowEngine(registry).run(workflow, context)

    assert run.success
    event_types = [item.get("event_type") for item in observed]
    assert "command_started" in event_types
    assert "command_spawned" in event_types
    assert "command_output" in event_types
    assert "command_heartbeat" in event_types
    assert "command_finished" in event_types
    assert any(item.get("terminal_line") == "linea-uno" for item in observed)
    assert any(item.get("terminal_line") == "linea-dos" for item in observed)
    assert any(int(item.get("pid") or 0) > 0 for item in observed)

    log_path = Path(next(item["log_path"] for item in observed if item.get("log_path")))
    assert log_path.is_file()
    log = log_path.read_text(encoding="utf-8")
    assert "linea-uno" in log
    assert "linea-dos" in log
    assert "[heartbeat]" in log

    persisted = [json.loads(line) for line in Path(run.events_path).read_text(encoding="utf-8").splitlines()]
    runtime_events = [item for item in persisted if item.get("kind") == "runtime_event"]
    assert any(item["data"].get("event_type") == "command_output" for item in runtime_events)
    assert any(item["data"].get("terminal_line") == "linea-dos" for item in runtime_events)


def test_legacy_runner_module_was_removed() -> None:
    project_root = Path(__file__).resolve().parents[1]
    assert not (project_root / "styler" / "runner.py").exists()


def test_pipecraft_is_the_only_python_process_boundary() -> None:
    project_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for source in (project_root / "styler").rglob("*.py"):
        relative = source.relative_to(project_root).as_posix()
        if relative == "styler/runtime/commands.py":
            continue
        text = source.read_text(encoding="utf-8")
        if "import subprocess" in text or '__import__("subprocess")' in text:
            offenders.append(relative)
        if "subprocess.run(" in text or "subprocess.Popen(" in text:
            offenders.append(relative)
    assert offenders == []


def test_change_screen_keeps_bars_and_adds_live_console() -> None:
    project_root = Path(__file__).resolve().parents[1]
    app_source = (project_root / "styler/tui/app.py").read_text(encoding="utf-8")
    css = (project_root / "styler/tui/styles/screens.tcss").read_text(encoding="utf-8")

    assert 'ProgressBar(total=100, id="overall-progress")' in app_source
    assert 'ProgressBar(total=100, id="phase-progress")' in app_source
    assert 'RichLog(' in app_source
    assert 'id="change-live-log"' in app_source
    assert 'id="change-process-command"' in app_source
    assert 'id="change-log-path"' in app_source
    assert "#change-live-log" in css
    assert "#change-process-command" in css
