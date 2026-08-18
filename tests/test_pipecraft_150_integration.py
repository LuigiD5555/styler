from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


from styler.pipecraft.compiler import compile_spec
from styler.pipecraft.engine import PipeCraftBackend
from styler.pipecraft.plugin_host import _runtime_status
from styler.pipecraft.service import PipeCraftUnavailable
from styler.workflow import WorkflowPlanner
from styler.planning.models import ExecutionContext, StepDefinition, WorkflowDefinition
from styler.planning.selection import select_plan_nodes


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
    engine = WorkflowPlanner()
    plan = engine.compile(workflow)
    ctx = ExecutionContext(root=tmp_path, dry_run=False, approve=True, values={"change_id": "x", "home": tmp_path})
    selected = select_plan_nodes(plan, ctx)
    name, raw = compile_spec(workflow, plan, ctx, selected)
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


def test_productive_execution_fails_closed_when_pipecraft_is_not_available(monkeypatch, tmp_path: Path) -> None:
    import pytest
    from styler import workflow as workflow_runtime
    import styler.pipecraft.service as service

    monkeypatch.setattr(workflow_runtime, "_execution_backend", workflow_runtime._pipecraft_execute)
    monkeypatch.setattr(service, "locate_binary", lambda: None)
    monkeypatch.setattr(service.PipeCraftClient, "ping", lambda self: (_ for _ in ()).throw(service.PipeCraftIpcError("offline")))
    workflow = WorkflowDefinition(name="demo", steps=[StepDefinition("a", "note", config={"message": "ok"})])
    with pytest.raises(PipeCraftUnavailable):
        workflow_runtime.execute(workflow, ExecutionContext(root=tmp_path, dry_run=False, approve=True))


def test_product_package_has_no_local_scheduler() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not (root / "styler/runtime/scheduler.py").exists()
    assert not (root / "styler/runtime/events.py").exists()


def test_pipecraft_status_normalization_preserves_styler_semantics() -> None:
    assert _runtime_status(True, "reconciled") == "ok"
    assert _runtime_status(True, "rolled_back") == "ok"
    assert _runtime_status(False, "requires_reboot") == "failed"
    assert _runtime_status(True, "ok_with_warnings") == "ok_with_warnings"


def test_report_restores_styler_status_from_plugin_data() -> None:
    workflow = WorkflowDefinition(name="demo", steps=[StepDefinition("a", "note")])
    plan = WorkflowPlanner().compile(workflow)
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


def test_plugin_host_argv_uses_installed_styler_entrypoint(monkeypatch, tmp_path: Path) -> None:
    import os
    import styler.pipecraft.compiler as compiler

    entry = tmp_path / "styler"
    entry.write_text("#!/bin/sh\n", encoding="utf-8")
    entry.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(entry)])
    assert compiler._plugin_host_argv() == [str(entry.resolve()), "__pipecraft_plugin_host"]


def test_plugin_host_argv_uses_zipapp_entrypoint(monkeypatch, tmp_path: Path) -> None:
    import styler.pipecraft.compiler as compiler

    archive = tmp_path / "styler.pyz"
    archive.write_bytes(b"placeholder")
    monkeypatch.setattr(sys, "argv", [str(archive)])
    assert compiler._plugin_host_argv() == [sys.executable, str(archive.resolve()), "__pipecraft_plugin_host"]


def test_compiler_does_not_write_transient_yaml(tmp_path: Path) -> None:
    workflow = WorkflowDefinition(name="pure", steps=[StepDefinition("a", "note")])
    plan = WorkflowPlanner().compile(workflow)
    ctx = ExecutionContext(root=tmp_path)
    selected = select_plan_nodes(plan, ctx)
    _name, spec = compile_spec(workflow, plan, ctx, selected)
    assert spec["schema_version"] == "pipecraft/v1"
    assert list(tmp_path.rglob("*.yaml")) == []


def test_explicit_command_node_uses_native_pipecraft_command(tmp_path: Path) -> None:
    workflow = WorkflowDefinition(
        name="native-command",
        steps=[
            StepDefinition(
                "echo",
                "command",
                config={"argv": ["printf", "%s", "hello"], "env": {"LANG": "C"}},
                timeout=7,
                retries=2,
            )
        ],
    )
    plan = WorkflowPlanner().compile(workflow)
    ctx = ExecutionContext(root=tmp_path)
    _name, spec = compile_spec(workflow, plan, ctx, {"echo"})
    step = spec["steps"][0]
    assert step["type"] == "command"
    assert step["with"]["argv"] == ["printf", "%s", "hello"]
    assert step["with"]["env"] == {"LANG": "C"}
    assert step["with"]["timeout"] == 7
    assert step["with"]["retries"] == 2
    assert "styler_step" not in step["with"]
