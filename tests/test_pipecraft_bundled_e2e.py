from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from styler import workflow as workflow_runtime
from styler.pipecraft.client import PipeCraftClient
from styler.pipecraft.service import locate_binary, prepare_workspace
from styler.planning.models import ExecutionContext, StepDefinition, WorkflowDefinition


@pytest.mark.skipif(os.name != "posix", reason="el runtime incluido actual es Linux x86_64")
def test_bundled_pipecraft_executes_a_real_styler_plugin(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch
) -> None:
    binary = locate_binary()
    if binary is None:
        pytest.skip("el checkout fuente no incluye el binario PipeCraft")

    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    monkeypatch.setenv("PYTHONPATH", str(repo_root))

    styler_root = tmp_path_factory.mktemp("pc")
    workspace = prepare_workspace(styler_root)
    process = subprocess.Popen(
        [str(binary), "--root", str(workspace), "serve", "--recovery", "manual"],
        cwd=workspace,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    client = PipeCraftClient(workspace)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                info = client.ping()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        assert info["protocol"] == "pipecraft.ipc/v1"

        # El fixture global usa un backend local para pruebas unitarias; este test
        # restablece deliberadamente la ruta productiva real.
        monkeypatch.setattr(workflow_runtime, "_execution_backend", workflow_runtime._pipecraft_execute)
        run = workflow_runtime.execute(
            WorkflowDefinition(
                name="e2e-note",
                steps=[StepDefinition("hello", "note", config={"message": "pipe-ok"})],
            ),
            ExecutionContext(root=styler_root, dry_run=False, approve=True),
        )
        assert run.success is True
        assert run.results[0].message == "pipe-ok"
        assert run.results[0].step_type == "note"
        # El YAML de catálogo sólo sirve para submit; PipeCraft conserva el
        # snapshot durable asociado al run y Styler elimina la copia temporal.
        assert Path(run.plan_path).name == "pipeline.snapshot.yaml"
        assert Path(run.plan_path).is_file()
        transient = workspace / ".pipelines" / "pipelines"
        assert not list(transient.glob("styler-e2e-note-*.yaml"))
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
