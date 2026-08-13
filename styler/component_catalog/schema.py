"""Traduce un dict TOML crudo a ``ComponentDefinition``.

Separado de ``loader.py`` para que la traducción de esquema (campo por
campo, con ruta y nombre de campo en cada error) pueda probarse sin tocar
disco.
"""
from __future__ import annotations

from typing import Any

from styler.component_catalog.errors import CatalogParseError
from styler.component_catalog.models import (
    SUPPORTED_SCHEMA_VERSIONS,
    ComponentDefinition,
    CompatibilityDefinition,
    InstallDefinition,
    ProviderDefinition,
    ResourceDefinition,
    RollbackDefinition,
    VerificationDefinition,
)

REQUIRED_FIELDS = ("schema_version", "id", "name", "kind")


def _field_error(path: str, field_name: str, message: str) -> CatalogParseError:
    return CatalogParseError(path, f"campo '{field_name}': {message}")


def _as_str_tuple(value: Any, path: str, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _field_error(path, field_name, "se esperaba una lista de cadenas")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise _field_error(path, field_name, f"valor no textual: {item!r}")
        result.append(item)
    return tuple(result)


def _parse_providers(data: dict[str, Any], path: str) -> tuple[ProviderDefinition, ...]:
    raw_providers = data.get("providers", {})
    if not isinstance(raw_providers, dict):
        raise _field_error(path, "providers", "se esperaba una tabla de proveedores")
    providers = []
    for provider_id, raw in raw_providers.items():
        if not isinstance(raw, dict):
            raise _field_error(path, f"providers.{provider_id}", "se esperaba una tabla")
        providers.append(
            ProviderDefinition(
                id=str(provider_id),
                type=str(raw.get("type", "")),
                families=_as_str_tuple(raw.get("families"), path, f"providers.{provider_id}.families"),
                packages=_as_str_tuple(raw.get("packages"), path, f"providers.{provider_id}.packages"),
                application_id=str(raw.get("application_id", "")),
                source=str(raw.get("source", "")),
                config_root=str(raw.get("config_root", "")),
                priority=int(raw.get("priority", 0)),
            )
        )
    return tuple(providers)


def _parse_resources(data: dict[str, Any], path: str) -> ResourceDefinition:
    raw = data.get("resources", {})
    if not isinstance(raw, dict):
        raise _field_error(path, "resources", "se esperaba una tabla")
    raw_paths = raw.get("paths", {})
    if not isinstance(raw_paths, dict):
        raise _field_error(path, "resources.paths", "se esperaba una tabla recurso -> ruta")
    return ResourceDefinition(
        exclusive=_as_str_tuple(raw.get("exclusive"), path, "resources.exclusive"),
        shared=_as_str_tuple(raw.get("shared"), path, "resources.shared"),
        paths={str(key): str(value) for key, value in raw_paths.items()},
    )


def _parse_verification(data: dict[str, Any], path: str) -> VerificationDefinition:
    raw = data.get("verification", {})
    if not isinstance(raw, dict):
        raise _field_error(path, "verification", "se esperaba una tabla")
    return VerificationDefinition(checks=_as_str_tuple(raw.get("checks"), path, "verification.checks"))


def _parse_rollback(data: dict[str, Any], path: str) -> RollbackDefinition:
    raw = data.get("rollback", {})
    if not isinstance(raw, dict):
        raise _field_error(path, "rollback", "se esperaba una tabla")
    return RollbackDefinition(
        level=str(raw.get("level", "none")),
        strategy=str(raw.get("strategy", "")),
    )


def _parse_compatibility(data: dict[str, Any], path: str) -> CompatibilityDefinition:
    raw = data.get("compatibility", {})
    if not isinstance(raw, dict):
        raise _field_error(path, "compatibility", "se esperaba una tabla")
    return CompatibilityDefinition(
        wayland=str(raw.get("wayland", "supported")),
        xwayland=str(raw.get("xwayland", "supported")),
        x11=str(raw.get("x11", "supported")),
    )


def _parse_install(data: dict[str, Any], path: str) -> InstallDefinition:
    raw = data.get("install", {})
    if not isinstance(raw, dict):
        raise _field_error(path, "install", "se esperaba una tabla")
    return InstallDefinition(
        pre_steps=_as_str_tuple(raw.get("pre_steps"), path, "install.pre_steps"),
        post_steps=_as_str_tuple(raw.get("post_steps"), path, "install.post_steps"),
    )


def parse_component(data: dict[str, Any], path: str) -> ComponentDefinition:
    """Convierte el dict de un TOML ya parseado en un ``ComponentDefinition``.

    No asigna ``source``: eso lo hace el loader, que sabe de qué nivel y
    archivo vino cada definición.
    """
    for name in REQUIRED_FIELDS:
        if name not in data:
            raise _field_error(path, name, "campo obligatorio ausente")

    schema_version = data["schema_version"]
    if not isinstance(schema_version, int):
        raise _field_error(path, "schema_version", "debe ser un entero")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise _field_error(
            path,
            "schema_version",
            f"versión {schema_version} no soportada (soportadas: {SUPPORTED_SCHEMA_VERSIONS})",
        )

    component_id = str(data["id"])
    if not component_id:
        raise _field_error(path, "id", "no puede estar vacío")
    name = str(data["name"])
    if not name:
        raise _field_error(path, "name", "no puede estar vacío")
    kind = str(data["kind"])
    if not kind:
        raise _field_error(path, "kind", "no puede estar vacío")

    return ComponentDefinition(
        schema_version=schema_version,
        id=component_id,
        name=name,
        kind=kind,
        description=str(data.get("description", "")),
        requires=_as_str_tuple(data.get("requires"), path, "requires"),
        optional_requires=_as_str_tuple(data.get("optional_requires"), path, "optional_requires"),
        provides=_as_str_tuple(data.get("provides"), path, "provides"),
        conflicts=_as_str_tuple(data.get("conflicts"), path, "conflicts"),
        providers=_parse_providers(data, path),
        resources=_parse_resources(data, path),
        install=_parse_install(data, path),
        verification=_parse_verification(data, path),
        rollback=_parse_rollback(data, path),
        compatibility=_parse_compatibility(data, path),
        criticality=str(data.get("criticality", "required")),
        messages={str(k): str(v) for k, v in (data.get("messages", {}) or {}).items()},
        capability_alias=str(data.get("capability_alias", "")),
    )
