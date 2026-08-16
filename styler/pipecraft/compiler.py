"""Compila un ExecutionPlan de Styler a un pipeline PipeCraft 1.5 transitorio."""
from __future__ import annotations

import math
import re
import sys
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from styler.runtime.models import ExecutionContext, ExecutionPlan, WorkflowDefinition


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items() if not callable(v)}
    if isinstance(value, (list, tuple, set)):
        return [_safe(v) for v in value if not callable(v)]
    if is_dataclass(value):
        return _safe(asdict(value))
    # Los objetos Python no serializables (drivers, callbacks, runners) son de
    # proceso y no deben cruzar la frontera IPC.
    return None


def _name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-") or "styler"
    return safe[:80]


def compile_pipeline(
    workflow: WorkflowDefinition,
    plan: ExecutionPlan,
    context: ExecutionContext,
    selected: set[str],
    pipeline_path: Path,
) -> str:
    pipeline_name = _name(f"styler-{workflow.name}-{context.values.get('change_id', '')}-{uuid.uuid4().hex[:10]}")
    context_values = _safe(context.values)
    if not isinstance(context_values, dict):
        context_values = {}
    context_values.pop("progress_callback", None)
    context_values.pop("command_runner", None)
    context_values["styler_root"] = str(context.root)
    context_values["continuation_mode"] = bool(context.values.get("continuation_mode", False))

    selected_nodes = [node for node in plan.nodes if node.id in selected]
    steps: list[dict[str, Any]] = []
    policy_steps: dict[str, str] = {}
    for node in selected_nodes:
        step = node.step
        policy, _source = workflow.on_error.resolve(node, "failed")
        policy_steps[node.id] = policy
        with_values: dict[str, Any] = {
            "argv": [sys.executable, "-m", "styler.pipecraft.plugin_host"],
            "styler_step": _safe(asdict(step)),
            "styler_node": {
                "id": node.id,
                "source_id": node.source_id,
                "kind": node.kind,
                "phase": node.phase,
                "block": node.block,
                "generated": node.generated,
            },
            "styler_context": context_values,
        }
        if step.timeout is not None:
            with_values["timeout"] = max(1, int(math.ceil(float(step.timeout))))
        if step.retries:
            with_values["retries"] = max(0, int(step.retries))
        if step.retry_delay:
            with_values["retry_delay"] = max(0, int(math.ceil(float(step.retry_delay))))
        idle = step.config.get("inactivity_timeout", step.config.get("idle_timeout"))
        if idle:
            try:
                with_values["inactivity_timeout"] = max(1, int(math.ceil(float(idle))))
            except (TypeError, ValueError):
                pass
        steps.append({
            "id": node.id,
            "type": "plugin",
            "description": step.description,
            "risk": step.risk,
            "required": step.required,
            "requires_approval": step.requires_approval,
            "needs": [dep for dep in node.needs if dep in selected],
            "run_if": node.run_if,
            "requires": list(step.requires),
            "provides": list(step.provides),
            "exclusive_resources": list(step.exclusive_resources),
            "shared_resources": list(step.shared_resources),
            "barrier": bool(step.barrier),
            "with": with_values,
        })

    document = {
        "schema_version": "pipecraft/v1",
        "name": pipeline_name,
        "description": f"Pipeline transitorio compilado por Styler para {workflow.name}",
        "context": {"styler": True, "operation": workflow.operation},
        "steps": steps,
        "on_error": {
            "default": workflow.on_error.default,
            "steps": policy_steps,
        },
    }
    pipeline_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = pipeline_path.with_suffix(pipeline_path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(pipeline_path)
    return pipeline_name
