"""Registro consultable de componentes ya cargados.

``ComponentRegistry`` no lee disco: se construye a partir de un
``LoadReport`` (o se llena a mano en pruebas) y mantiene los índices
inversos que el resolver y el validador necesitan (capacidad → proveedores,
capacidad → consumidores, tipo → componentes, origen → componentes).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from styler.component_catalog.errors import DuplicateComponentError
from styler.component_catalog.loader import LoadReport
from styler.component_catalog.models import ComponentDefinition, ComponentSource


@dataclass
class ComponentRegistry:
    _by_id: dict[str, ComponentDefinition] = field(default_factory=dict)
    _by_capability_provider: dict[str, set[str]] = field(default_factory=dict)
    _by_capability_consumer: dict[str, set[str]] = field(default_factory=dict)
    _by_kind: dict[str, set[str]] = field(default_factory=dict)
    _by_source_level: dict[str, set[str]] = field(default_factory=dict)

    # -- construcción ------------------------------------------------
    @classmethod
    def from_report(cls, report: LoadReport) -> "ComponentRegistry":
        registry = cls()
        for loaded in report.components.values():
            registry.register(loaded.definition)
        return registry

    def register(self, component: ComponentDefinition) -> None:
        if component.id in self._by_id:
            existing = self._by_id[component.id]
            existing_path = existing.source.path if existing.source else "?"
            new_path = component.source.path if component.source else "?"
            raise DuplicateComponentError(component.id, existing_path, new_path)
        self._index(component)

    def override(self, component: ComponentDefinition) -> None:
        """Reemplaza una definición existente (o la agrega si no existía)."""
        if component.id in self._by_id:
            self._deindex(component.id)
        self._index(component)

    def _index(self, component: ComponentDefinition) -> None:
        self._by_id[component.id] = component
        for capability in component.provides:
            self._by_capability_provider.setdefault(capability, set()).add(component.id)
        for capability in (*component.requires, *component.optional_requires):
            self._by_capability_consumer.setdefault(capability, set()).add(component.id)
        self._by_kind.setdefault(component.kind, set()).add(component.id)
        level = component.source.level if component.source else "unknown"
        self._by_source_level.setdefault(level, set()).add(component.id)

    def _deindex(self, component_id: str) -> None:
        component = self._by_id.pop(component_id, None)
        if component is None:
            return
        for capability in component.provides:
            self._by_capability_provider.get(capability, set()).discard(component_id)
        for capability in (*component.requires, *component.optional_requires):
            self._by_capability_consumer.get(capability, set()).discard(component_id)
        self._by_kind.get(component.kind, set()).discard(component_id)
        level = component.source.level if component.source else "unknown"
        self._by_source_level.get(level, set()).discard(component_id)

    # -- consultas -----------------------------------------------------
    def get(self, component_id: str) -> ComponentDefinition | None:
        return self._by_id.get(component_id)

    def all(self) -> tuple[ComponentDefinition, ...]:
        return tuple(self._by_id.values())

    def contains(self, component_id: str) -> bool:
        return component_id in self._by_id

    def providers_for(self, capability: str) -> tuple[ComponentDefinition, ...]:
        return tuple(self._by_id[cid] for cid in sorted(self._by_capability_provider.get(capability, ())))

    def consumers_of(self, capability: str) -> tuple[ComponentDefinition, ...]:
        return tuple(self._by_id[cid] for cid in sorted(self._by_capability_consumer.get(capability, ())))

    def dependents_of(self, component_id: str) -> tuple[ComponentDefinition, ...]:
        component = self._by_id.get(component_id)
        if component is None:
            return ()
        capabilities = set(component.provides)
        dependents: set[str] = set()
        for capability in capabilities:
            dependents.update(self._by_capability_consumer.get(capability, ()))
        dependents.discard(component_id)
        return tuple(self._by_id[cid] for cid in sorted(dependents))

    def source_of(self, component_id: str) -> ComponentSource | None:
        component = self._by_id.get(component_id)
        return component.source if component else None

    def by_kind(self, kind: str) -> tuple[ComponentDefinition, ...]:
        return tuple(self._by_id[cid] for cid in sorted(self._by_kind.get(kind, ())))
