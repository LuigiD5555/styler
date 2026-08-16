from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from styler.pipecraft.compiler import compile_pipeline
from styler.pipecraft.engine import PipeCraftBackend
from styler.pipecraft.plugin_host import _runtime_status
from styler.runtime.engine import WorkflowEngine
from styler.runtime.models import ExecutionContext, StepDefinition, WorkflowDefinition
from styler.runtime.selection import select_plan_nodes


def test_compiler_turns_styler_nodes_into_pipecraft_plugins(tmp_path: Path) -> None:
    workflow = WorkflowDefinition(
        name="demo",
        steps=[
            StepDefinition(
                "a", "note", description="A", provides=["ready"], exclusive_resources=["db"],
            ),
            StepDefinition(
                "b", "note", description="B", needs=["a"], requires=["ready"], shared_resources=["network"],
            ),
        ],
    )
    engine = WorkflowEngine()
    plan = engine.compile(workflow)
    ctx = ExecutionContext(root=tmp_path, dry_run=False, approve=True, values={"change_id": "x", "home": tmp_path})
    selected = select_plan_nodes(plan, ctx)
    path = tmp_path / "pipeline.yaml"
    name = compile_pipeline(workflow, plan, ctx, selected, path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert name.startswith("styler-demo")
    assert [step["type"] for step in raw["steps"]] == ["plugin", "plugin"]
    assert raw["steps"][1]["needs"] == ["a"]
    assert raw["steps"][0]["exclusive_resources"] == ["db"]
    assert raw["steps"][1]["shared_resources"] == ["network"]
    assert raw["steps"][1]["requires"] == ["ready"]
    assert raw["steps"][0]["with"]["styler_step"]["step_type"] == "note"
    assert raw["steps"][0]["with"]["styler_context"]["styler_root"] == str(tmp_path)


def test_plugin_host_executes_styler_executor_without_polluting_stdout(tmp_path: Path) -> None:
    payload = {
        "protocol": "pipecraft.plugin/v1",
        "run_id": "r1",
        "pipeline": "demo",
        "step_id": "hello",
        "root": str(tmp_path),
        "cwd": str(tmp_path),
        "labels": [],
        "dry_run": False,
        "with": {
            "styler_step": {"id": "hello", "step_type": "note", "description": "Hola", "config": {"message": "Hola PipeCraft"}},
            "styler_node": {"id": "hello", "source_id": "hello", "kind": "action"},
            "styler_context": {"styler_root": str(tmp_path)},
        },
        "artifacts_dir": str(tmp_path / "artifacts"),
        "logs_dir": str(tmp_path / "logs"),
    }
    completed = subprocess.run(
        [sys.executable, "-m", "styler.pipecraft.plugin_host"],
        input=json.dumps(payload), text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["success"] is True
    assert result["message"] == "Hola PipeCraft"
    assert result["data"]["styler_step_type"] == "note"


def test_auto_backend_stays_local_when_pipecraft_is_not_available(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STYLER_RUNTIME", "local")
    workflow = WorkflowDefinition(name="demo", steps=[StepDefinition("a", "note", config={"message": "ok"})])
    run = WorkflowEngine(backend="auto").run(workflow, ExecutionContext(root=tmp_path, dry_run=False, approve=True))
    assert run.success is True
    assert run.results[0].message == "ok"


def test_pipecraft_status_normalization_preserves_styler_semantics() -> None:
    assert _runtime_status(True, "reconciled") == "ok"
    assert _runtime_status(True, "rolled_back") == "ok"
    assert _runtime_status(False, "requires_reboot") == "failed"
    assert _runtime_status(True, "ok_with_warnings") == "ok_with_warnings"


def test_report_restores_styler_status_from_plugin_data() -> None:
    workflow = WorkflowDefinition(name="demo", steps=[StepDefinition("a", "note")])
    plan = WorkflowEngine().compile(workflow)
    report = {
        "results": [{
            "step_id": "a",
            "success": True,
            "status": "ok",
            "message": "reconciliado",
            "output": "",
            "data": {"styler_status": "reconciled", "styler_step_type": "note"},
        }]
    }
    results = PipeCraftBackend._results(report, plan)
    assert results[0].status == "reconciled"
    assert results[0].success is True
