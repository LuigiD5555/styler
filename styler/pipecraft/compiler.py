"""Compilador puro Styler -> PipeCraft.

Este módulo no escribe archivos ni conoce el transporte IPC. Su única tarea es
traducir el significado de un :class:`ExecutionPlan` de Styler a una spec
``pipecraft/v1`` serializable. PipeCraft decide cómo validarla, planificarla y
ejecutarla.
"""
from __future__ import annotations

import math
import os
import re
import sys
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from styler.planning.models import ExecutionContext, ExecutionPlan, WorkflowDefinition


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
    # Drivers, callbacks y runners son objetos del proceso de Styler y nunca
    # deben cruzar la frontera con PipeCraft.
    return None


def _name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-") or "styler"
    return safe[:80]


def _plugin_host_argv() -> list[str]:
    """Devuelve un plugin-host válido en wheel, zipapp y ejecución desde source."""
    try:
        entry = Path(sys.argv[0]).expanduser()
    except (TypeError, ValueError):
        entry = Path()
    if entry.is_file():
        if entry.suffix == ".pyz":
            return [sys.executable, str(entry.resolve()), "__pipecraft_plugin_host"]
        if entry.name == "styler" and os.access(entry, os.X_OK):
            return [str(entry.resolve()), "__pipecraft_plugin_host"]
    return [sys.executable, "-m", "styler.execution.plugin_host"]


def _runtime_controls(step) -> dict[str, Any]:
    controls: dict[str, Any] = {}
    if step.timeout is not None:
        controls["timeout"] = max(1, int(math.ceil(float(step.timeout))))
    if step.retries:
        controls["retries"] = max(0, int(step.retries))
    if step.retry_delay:
        controls["retry_delay"] = max(0, int(math.ceil(float(step.retry_delay))))
    idle = step.config.get("inactivity_timeout", step.config.get("idle_timeout"))
    if idle:
        try:
            controls["inactivity_timeout"] = max(1, int(math.ceil(float(idle))))
        except (TypeError, ValueError):
            pass
    return controls


def _common_step(node, selected: set[str]) -> dict[str, Any]:
    step = node.step
    return {
        "id": node.id,
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
    }


def _command_with(step) -> dict[str, Any]:
    """Compila un nodo explícitamente ``command`` al executor Rust nativo.

    Un comando directo sólo es válido cuando el autor del workflow lo declaró
    así. No intentamos adivinar que un executor semántico de Styler "en realidad"
    es un comando porque eso perdería receipts, reconciliación o política.
    """
    argv = step.config.get("argv")
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(v, str) and v for v in argv):
        raise ValueError(f"El step command {step.id!r} requiere config.argv como lista no vacía de strings")

    values: dict[str, Any] = {"argv": list(argv)}
    env = step.config.get("env")
    if isinstance(env, dict) and env:
        values["env"] = {str(k): str(v) for k, v in env.items()}
    cwd = step.config.get("cwd")
    if cwd:
        values["cwd"] = str(cwd)
    values.update(_runtime_controls(step))
    return values


def _plugin_with(step, node, context_values: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {
        "argv": _plugin_host_argv(),
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
    values.update(_runtime_controls(step))
    return values


def compile_spec(
    workflow: WorkflowDefinition,
    plan: ExecutionPlan,
    context: ExecutionContext,
    selected: set[str],
) -> tuple[str, dict[str, Any]]:
    """Devuelve ``(pipeline_name, spec)`` sin tocar el filesystem."""
    pipeline_name = _name(
        f"styler-{workflow.name}-{context.values.get('change_id', '')}-{uuid.uuid4().hex[:10]}"
    )
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
        compiled = _common_step(node, selected)
        if step.step_type == "command":
            compiled["type"] = "command"
            compiled["with"] = _command_with(step)
        else:
            compiled["type"] = "plugin"
            compiled["with"] = _plugin_with(step, node, context_values)
        steps.append(compiled)

    document: dict[str, Any] = {
        "schema_version": "pipecraft/v1",
        "name": pipeline_name,
        "description": f"Pipeline compilado por Styler para {workflow.name}",
        "context": {"styler": True, "operation": workflow.operation},
        "steps": steps,
        "on_error": {
            "default": workflow.on_error.default,
            "steps": policy_steps,
        },
    }
    return pipeline_name, document
