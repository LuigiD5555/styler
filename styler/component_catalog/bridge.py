"""Consulta directa de metadatos del catálogo para vistas de impacto."""
from __future__ import annotations

from dataclasses import dataclass

from styler.component_catalog.loader import load
from styler.component_catalog.registry import ComponentRegistry

_cached_registry: ComponentRegistry | None = None


def _registry() -> ComponentRegistry:
    global _cached_registry
    if _cached_registry is None:
        _cached_registry = ComponentRegistry.from_report(load(root="."))
    return _cached_registry


def reset_cache() -> None:
    """Fuerza a releer el catálogo en la próxima consulta."""
    global _cached_registry
    _cached_registry = None


@dataclass(frozen=True)
class CatalogNote:
    component_id: str
    rollback_level: str
    rollback_strategy: str
    verification_checks: tuple[str, ...]

    def as_text(self) -> str:
        parts = [f"catálogo: rollback {self.rollback_level}"]
        if self.rollback_strategy:
            parts.append(f"({self.rollback_strategy})")
        if self.verification_checks:
            parts.append(f"— verifica: {', '.join(self.verification_checks)}")
        return " ".join(parts)


def catalog_note_for(capability_or_type: str) -> CatalogNote | None:
    """Devuelve metadatos del componente que declara la capacidad indicada."""
    for component in _registry().all():
        if capability_or_type not in {
            component.id,
            component.capability_alias,
            *component.provides,
        }:
            continue
        return CatalogNote(
            component_id=component.id,
            rollback_level=component.rollback.level,
            rollback_strategy=component.rollback.strategy,
            verification_checks=component.verification.checks,
        )
    return None
