"""Serialización segura de workflows para paquetes portables."""
from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any, Mapping, Sequence

from styler.component_catalog.executors import extended_registry
from styler.runtime.engine import WorkflowEngine
from styler.runtime.models import (
    CheckAttachment,
    CheckReference,
    DependencyMode,
    ErrorPolicy,
    HookDefinition,
    HookFilter,
    HookSet,
    PhaseDefinition,
    RunCondition,
    StepDefinition,
    WorkflowDefinition,
    WorkflowOperation,
)

from .models import PortablePackageError

# Ningún executor actual interpreta estas claves como shell, pero se bloquean
# para que el formato no adquiera esa superficie accidentalmente en el futuro.
_PATH_CONFIG_KEYS = frozenset({"path", "paths", "target", "base", "config_root", "destination"})
_FORBIDDEN_CONFIG_KEYS = frozenset({
    "argv", "command", "commands", "shell", "script", "python", "executable", "interpreter"
})


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PortablePackageError(f"'{label}' debe ser un objeto.")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PortablePackageError(f"'{label}' debe ser una lista.")
    return value


def _filter_kwargs(cls: type, data: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {field.name for field in fields(cls)}
    return {key: value for key, value in data.items() if key in allowed}


def _check_reference(data: Mapping[str, Any]) -> CheckReference:
    reference = CheckReference(**_filter_kwargs(CheckReference, data))
    _audit_config(reference.with_values, f"check.{reference.uses}.with_values")
    return reference


def _check_attachment(data: Mapping[str, Any] | None) -> CheckAttachment:
    raw = data or {}
    return CheckAttachment(
        before=[_check_reference(_mapping(item, "checks.before[]")) for item in raw.get("before", [])],
        after=[_check_reference(_mapping(item, "checks.after[]")) for item in raw.get("after", [])],
        on_failure=[
            _check_reference(_mapping(item, "checks.on_failure[]"))
            for item in raw.get("on_failure", [])
        ],
    )


def _hook_filter(data: Mapping[str, Any] | None) -> HookFilter:
    return HookFilter(**_filter_kwargs(HookFilter, data or {}))


def _hook(data: Mapping[str, Any]) -> HookDefinition:
    kwargs = _filter_kwargs(HookDefinition, data)
    kwargs["match"] = _hook_filter(_mapping(data.get("match", {}), "hook.match"))
    kwargs["except_filter"] = _hook_filter(
        _mapping(data.get("except_filter", {}), "hook.except_filter")
    )
    hook = HookDefinition(**kwargs)
    _audit_config(hook.with_values, f"hook.{hook.id}.with_values")
    if hook.run_if not in RunCondition.ALL:
        raise PortablePackageError(
            f"El hook '{hook.id}' declara run_if desconocido: '{hook.run_if}'."
        )
    return hook


def _hooks(data: Mapping[str, Any] | None) -> HookSet:
    raw = data or {}
    kwargs: dict[str, Any] = {}
    for field in fields(HookSet):
        items = raw.get(field.name, [])
        kwargs[field.name] = [_hook(_mapping(item, f"hooks.{field.name}[]")) for item in items]
    return HookSet(**kwargs)


def _step(data: Mapping[str, Any]) -> StepDefinition:
    kwargs = _filter_kwargs(StepDefinition, data)
    kwargs["id"] = str(data.get("id", ""))
    kwargs["step_type"] = str(data.get("step_type", ""))
    kwargs["checks"] = _check_attachment(_mapping(data.get("checks", {}), "step.checks"))
    step = StepDefinition(**kwargs)
    if not step.id or not step.step_type:
        raise PortablePackageError("Cada nodo del grafo necesita 'id' y 'step_type'.")
    _audit_config(step.config, f"steps.{step.id}.config")
    _audit_config(step.rollback, f"steps.{step.id}.rollback")
    _audit_config(step.observe, f"steps.{step.id}.observe")
    _audit_config(step.inputs, f"steps.{step.id}.inputs")
    _audit_config(step.outputs, f"steps.{step.id}.outputs")
    return step


def _audit_config(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized_key = str(key).lower()
            if normalized_key in _FORBIDDEN_CONFIG_KEYS:
                raise PortablePackageError(
                    f"El grafo declara la clave reservada '{key}' en {path}. "
                    "Un paquete portable no puede introducir comandos arbitrarios."
                )
            if normalized_key in _PATH_CONFIG_KEYS:
                candidates = child if isinstance(child, list) else [child]
                for candidate in candidates:
                    if not isinstance(candidate, str):
                        continue
                    if ".." in candidate.replace("\\", "/").split("/"):
                        raise PortablePackageError(
                            f"La ruta '{candidate}' escapa mediante '..' en {path}.{key}."
                        )
                    if candidate.startswith("/") and not candidate.startswith("${HOME}/"):
                        raise PortablePackageError(
                            f"La ruta absoluta '{candidate}' no es portable en {path}.{key}."
                        )
            _audit_config(child, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            _audit_config(child, f"{path}[{index}]")


def workflow_from_portable_dict(data: Mapping[str, Any]) -> WorkflowDefinition:
    raw = _mapping(data, "workflow")
    raw_steps = _sequence(raw.get("steps", []), "workflow.steps")
    phases_raw = _mapping(raw.get("phases", {}), "workflow.phases")
    on_error_raw = _mapping(raw.get("on_error", {}), "workflow.on_error")
    workflow = WorkflowDefinition(
        name=str(raw.get("name", "")),
        description=str(raw.get("description", "")),
        operation=str(raw.get("operation", WorkflowOperation.GENERIC)),
        metadata=dict(_mapping(raw.get("metadata", {}), "workflow.metadata")),
        steps=[_step(_mapping(item, "workflow.steps[]")) for item in raw_steps],
        on_error=ErrorPolicy(**_filter_kwargs(ErrorPolicy, on_error_raw)),
        phases={
            str(name): PhaseDefinition(**_filter_kwargs(PhaseDefinition, _mapping(value, f"phases.{name}")))
            for name, value in phases_raw.items()
        },
        hooks=_hooks(_mapping(raw.get("hooks", {}), "workflow.hooks")),
        dependency_mode=str(raw.get("dependency_mode", DependencyMode.STRICT)),
        observations=dict(_mapping(raw.get("observations", {}), "workflow.observations")),
        outputs=dict(_mapping(raw.get("outputs", {}), "workflow.outputs")),
        schema_version=str(raw.get("schema_version", "pipecraft/v1")),
    )
    if not workflow.name.strip():
        raise PortablePackageError("El workflow necesita un nombre.")
    if workflow.operation not in WorkflowOperation.ALL:
        raise PortablePackageError(f"Operación de workflow desconocida: '{workflow.operation}'.")
    if workflow.dependency_mode not in DependencyMode.ALL:
        raise PortablePackageError(
            f"Modo de dependencias no soportado: '{workflow.dependency_mode}'."
        )
    for step in workflow.steps:
        if step.run_if not in RunCondition.ALL:
            raise PortablePackageError(
                f"El nodo '{step.id}' declara run_if desconocido: '{step.run_if}'."
            )
    try:
        WorkflowEngine(extended_registry()).compile(workflow)
    except ValueError as exc:
        raise PortablePackageError(f"El grafo no compila:\n{exc}") from exc
    return workflow


def workflow_to_portable_dict(workflow: WorkflowDefinition) -> dict[str, Any]:
    return asdict(workflow)
