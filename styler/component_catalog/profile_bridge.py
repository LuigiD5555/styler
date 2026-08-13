"""Puente entre perfiles capturados y el catálogo declarativo.

Permite que las decisiones del constructor de plan afecten a la restauración
real sin enseñar a ``restore.py`` a interpretar pantallas o TOML.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from styler.applications import AppSpec, merge_applications
from styler.component_catalog.models import ComponentDefinition, ProviderDefinition
from styler.component_catalog.plan_draft import PlanDraft
from styler.component_catalog.registry import ComponentRegistry
from styler.layers import Layer

KDE_PARTS = frozenset({"tema-colores", "paneles", "atajos", "konsole", "dolphin"})
PROVIDER_TO_RESTORE_MANAGER = {"rpm": "dnf"}


def _provider_matches_app(provider: ProviderDefinition, app: AppSpec) -> bool:
    values = {app.name.casefold(), app.identity.casefold()}
    values.discard("")
    candidates = {item.casefold() for item in provider.packages}
    if provider.application_id:
        candidates.add(provider.application_id.casefold())
    return bool(values & candidates)


def component_for_app(registry: ComponentRegistry, app: AppSpec) -> ComponentDefinition | None:
    for component in registry.all():
        if component.kind != "application":
            continue
        if any(_provider_matches_app(provider, app) for provider in component.providers):
            return component
    return None


def desired_components_for_layers(
    layers: Iterable[Layer],
    registry: ComponentRegistry,
) -> tuple[str, ...]:
    layers = list(layers)
    desired: list[str] = []

    for layer in layers:
        for app in layer.applications:
            component = component_for_app(registry, app)
            if component is not None:
                desired.append(component.id)
        for package in layer.packages:
            app = AppSpec(manager=package.manager, name=package.name, version=package.version)
            component = component_for_app(registry, app)
            if component is not None:
                desired.append(component.id)

        text = f"{layer.title} {layer.notes}".casefold()
        if layer.part_id == "gimp" and "photogimp" in text and registry.contains("app.photogimp"):
            desired.append("app.photogimp")
        if layer.part_id in KDE_PARTS and registry.contains("config.kde.user"):
            desired.append("config.kde.user")
        for environment in layer.desktop_environments:
            capability = f"desktop.{environment.environment_id}"
            for component in registry.all():
                if component.capability_alias == capability:
                    desired.append(component.id)

    return tuple(dict.fromkeys(desired))


def layer_component_ids(layer: Layer, registry: ComponentRegistry) -> set[str]:
    result: set[str] = set()
    text = f"{layer.title} {layer.notes}".casefold()
    if layer.part_id == "gimp" and "photogimp" in text and registry.contains("app.photogimp"):
        result.add("app.photogimp")
    if layer.part_id in KDE_PARTS and registry.contains("config.kde.user"):
        result.add("config.kde.user")
    for environment in layer.desktop_environments:
        capability = f"desktop.{environment.environment_id}"
        result.update(
            component.id
            for component in registry.all()
            if component.capability_alias == capability
        )
    return result


def filter_layers_for_draft(
    layers: Iterable[Layer],
    draft: PlanDraft,
    registry: ComponentRegistry,
) -> list[Layer]:
    result: list[Layer] = []
    for layer in layers:
        component_ids = layer_component_ids(layer, registry)
        if component_ids and component_ids <= draft.excluded_components:
            continue
        result.append(layer)
    return result


def app_from_provider(
    component: ComponentDefinition,
    provider: ProviderDefinition,
    original: AppSpec | None = None,
) -> AppSpec:
    manager = PROVIDER_TO_RESTORE_MANAGER.get(provider.type, provider.type)
    original = original or AppSpec(manager=manager, name=component.name)
    if provider.type == "flatpak" and provider.application_id:
        name = provider.application_id
        identity = provider.application_id
        remote = original.remote or "flathub"
    else:
        name = provider.packages[0] if provider.packages else (provider.application_id or original.name)
        identity = original.identity or provider.application_id
        remote = original.remote
    return replace(
        original,
        manager=manager,
        name=name,
        identity=identity,
        display_name=original.display_name or component.name,
        remote=remote,
        version="" if manager != original.manager else original.version,
        artifact_path="" if manager != original.manager else original.artifact_path,
        notes=(original.notes + " | proveedor elegido en el plan de Styler").strip(" |"),
    )


def applications_for_draft(
    layers: Iterable[Layer],
    draft: PlanDraft,
    registry: ComponentRegistry,
) -> list[AppSpec]:
    original_apps = merge_applications(layer.applications for layer in layers)
    originals_by_component: dict[str, AppSpec] = {}
    passthrough: list[AppSpec] = []
    for app in original_apps:
        component = component_for_app(registry, app)
        if component is None:
            passthrough.append(app)
            continue
        originals_by_component.setdefault(component.id, app)

    selected_ids: list[str] = []
    seen: set[str] = set()

    def add_component(component_id: str) -> None:
        if component_id in seen or component_id in draft.excluded_components:
            return
        component = registry.get(component_id)
        if component is None:
            return
        seen.add(component_id)
        selected_ids.append(component_id)
        for requirement in component.requires:
            candidates = registry.providers_for(requirement)
            if candidates:
                key = f"{component.id}|{requirement}"
                preferred_id = draft.preferred_components.get(key, "")
                chosen = next((item for item in candidates if item.id == preferred_id), candidates[0])
                add_component(chosen.id)

    for component_id in draft.desired_components:
        add_component(component_id)

    selected_application_ids = [
        component_id
        for component_id in selected_ids
        if (component := registry.get(component_id)) is not None
        and component.kind == "application"
    ]

    result = list(passthrough)
    for component_id in selected_application_ids:
        component = registry.get(component_id)
        if component is None:
            continue
        original = originals_by_component.get(component_id)
        provider_id = draft.preferred_providers.get(component_id, "")
        provider = next((item for item in component.providers if item.id == provider_id), None)
        if provider is None and original is not None:
            provider = next(
                (item for item in component.providers if _provider_matches_app(item, original)),
                None,
            )
        if provider is None:
            provider = next(iter(sorted(component.providers, key=lambda item: (-item.priority, item.id))), None)
        if provider is None:
            if original is not None:
                result.append(original)
            continue
        result.append(app_from_provider(component, provider, original))
    return merge_applications([result])
