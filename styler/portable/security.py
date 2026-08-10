"""Política de seguridad para acciones declarativas dentro de ``.stylerpkg``."""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from styler.automation.specs import ActionSpec, SpecError
from styler.portable.redaction import HOME_TOKEN, is_sensitive

PATH_PARAMS = ("path", "base", "target", "paths", "destination", "config_root")
UNTRUSTED_ACTION_KINDS = frozenset({"launch_process"})
UNTRUSTED_CONDITION_KINDS = frozenset({"command_output"})
FORBIDDEN_KEYS = frozenset({
    "argv", "command", "commands", "shell", "script", "python", "executable", "interpreter"
})


class PortableSecurityError(SpecError):
    """Una acción portable intenta ampliar la superficie de ejecución."""


def _check_path(raw: str) -> None:
    text = str(raw)
    if text.startswith(("package://", "catalog://")):
        relative = text.split("://", 1)[1]
        if not relative or ".." in PurePosixPath(relative).parts:
            raise PortableSecurityError(f"Ruta portable insegura: {text}")
        return
    candidate = text[len(HOME_TOKEN):] if text.startswith(HOME_TOKEN) else text
    if ".." in PurePosixPath(candidate).parts:
        raise PortableSecurityError(f"La acción declara una ruta con '..': {text}")
    if text.startswith("/") and not text.startswith(HOME_TOKEN):
        raise PortableSecurityError(
            f"La acción declara una ruta absoluta fuera del HOME: {text}. "
            "Usa ${HOME} o un recurso package://."
        )
    if is_sensitive(text):
        raise PortableSecurityError(f"La acción toca una ruta sensible: {text}")


def _audit_condition_paths(condition: Any) -> None:
    for key in PATH_PARAMS:
        value = condition.params.get(key)
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            _check_path(str(item))
    for child in condition.children:
        _audit_condition_paths(child)


def audit_paths(spec: ActionSpec) -> None:
    for node in spec.walk():
        for key in PATH_PARAMS:
            value = node.params.get(key)
            if value is None:
                continue
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values:
                _check_path(str(item))
        if node.condition is not None:
            _audit_condition_paths(node.condition)
        for condition in node.abort_conditions:
            _audit_condition_paths(condition)


def _audit_mapping(value: Any, label: str = "params") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise PortableSecurityError(
                    f"'{label}.{key}' no está permitido dentro de un .stylerpkg declarativo."
                )
            _audit_mapping(nested, f"{label}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _audit_mapping(nested, f"{label}[{index}]")


def _audit_condition_execution(condition: Any) -> None:
    if condition.kind in UNTRUSTED_CONDITION_KINDS:
        raise PortableSecurityError(
            f"La condición '{condition.kind}' no puede ejecutar comandos desde un paquete."
        )
    _audit_mapping(condition.params, f"condition.{condition.kind}")
    for child in condition.children:
        _audit_condition_execution(child)


def audit_execution_surface(spec: ActionSpec) -> None:
    for node in spec.walk():
        if node.kind in UNTRUSTED_ACTION_KINDS:
            raise PortableSecurityError(
                f"La acción '{node.kind}' no está permitida dentro de paquetes importados."
            )
        _audit_mapping(node.params, f"action.{node.kind}")
        if node.condition is not None:
            _audit_condition_execution(node.condition)
        for condition in node.abort_conditions:
            _audit_condition_execution(condition)
