"""Resolver de capacidades y proveedores (Fase 2).

Entrada: los componentes que el usuario quiere (por ID), la familia de
distribución destino (``target.family``: ubuntu, arch, fedora...),
preferencias del usuario (qué proveedor usar para un componente concreto)
y una política de proveedores permitidos.

Salida: ``ResolutionResult`` — componentes seleccionados (el conjunto
deseado más sus dependencias obligatorias), el proveedor elegido para cada
uno, qué capacidades quedaron satisfechas o faltantes, y una decisión
legible por cada requisito (por qué se eligió ese proveedor o por qué no
hay ninguno disponible).

Este módulo no instala nada ni construye el DAG: solo decide *qué* y
*cómo*. La traducción a nodos runtime es responsabilidad de
``compiler.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from styler.component_catalog.models import ComponentDefinition, ProviderDefinition
from styler.component_catalog.registry import ComponentRegistry

# Orden de criterios de selección de proveedor (sección 17 del encargo).
# No se elige solo por prioridad numérica: primero debe ser compatible con
# la familia destino y estar permitido por la política; solo entre los que
# cumplen eso se usa la preferencia del usuario y luego la prioridad.


@dataclass(frozen=True)
class ResolutionDecision:
    component_id: str
    requirement: str  # la capacidad que originó esta decisión ("" para el propio componente)
    candidates: tuple[str, ...]  # IDs de componentes que podrían proveerla
    chosen_component: str = ""
    chosen_provider: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "component_id": self.component_id,
            "requirement": self.requirement,
            "candidates": list(self.candidates),
            "chosen_component": self.chosen_component,
            "chosen_provider": self.chosen_provider,
            "reason": self.reason,
        }


@dataclass
class ResolutionResult:
    selected_components: tuple[str, ...] = ()
    # Componentes que la persona pidió explícitamente, no los que entraron por
    # cierre transitivo. Un componente pedido nunca puede fallar en silencio,
    # aunque su 'criticality' sea 'optional': esa marca solo autoriza al
    # resolutor a omitirlo del plan, no al motor a fingir que se aplicó.
    requested_components: tuple[str, ...] = ()
    selected_providers: dict[str, str] = field(default_factory=dict)  # component_id -> provider_id
    satisfied_capabilities: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()
    decisions: tuple[ResolutionDecision, ...] = ()

    @property
    def blocked_components(self) -> tuple[str, ...]:
        """Componentes deseados que no pueden instalarse (requisito obligatorio sin proveedor)."""
        return tuple(
            decision.component_id
            for decision in self.decisions
            if not decision.chosen_component and decision.requirement == ""
        )

    @property
    def ok(self) -> bool:
        return not self.missing_capabilities and not self.blocked_components


def _provider_ok(provider: ProviderDefinition, family: str, allowed_types: frozenset[str] | None) -> bool:
    if allowed_types is not None and provider.type not in allowed_types:
        return False
    if not provider.families:
        return True
    return family in provider.families or "*" in provider.families


def _choose_provider(
    component: ComponentDefinition,
    family: str,
    preferred_provider_id: str,
    allowed_types: frozenset[str] | None,
) -> tuple[ProviderDefinition | None, str]:
    """Devuelve (proveedor elegido, motivo legible) o (None, motivo)."""
    if not component.providers:
        return None, "el componente no declara proveedores (no instalable por sí mismo)"

    compatible = [p for p in component.providers if _provider_ok(p, family, allowed_types)]
    if not compatible:
        return None, (
            f"ningún proveedor de '{component.id}' es compatible con la familia "
            f"'{family}' bajo la política de proveedores permitidos"
        )

    if preferred_provider_id:
        for provider in compatible:
            if provider.id == preferred_provider_id:
                return provider, f"preferencia del usuario ('{preferred_provider_id}')"

    best = sorted(compatible, key=lambda p: (-p.priority, p.id))[0]
    return best, f"mayor prioridad compatible con '{family}' ({best.priority})"


def resolve(
    registry: ComponentRegistry,
    desired_component_ids: list[str] | tuple[str, ...],
    family: str,
    *,
    preferred_providers: dict[str, str] | None = None,
    preferred_components: dict[tuple[str, str], str] | None = None,
    allowed_provider_types: frozenset[str] | None = None,
) -> ResolutionResult:
    preferred_providers = dict(preferred_providers or {})
    preferred_components = preferred_components or {}

    # PhotoGIMP recomienda GIMP Flatpak/Flathub porque es la estrategia que
    # Styler puede integrar de extremo a extremo. Una elección explícita del
    # usuario (APT, Pacman, AUR, Snap...) se respeta y la capa de cambios
    # construye entonces una estrategia asistida en vez de fingir soporte total.
    if "app.photogimp" in desired_component_ids:
        preferred_providers.setdefault("app.gimp", "flatpak")

    # 1. Cierre transitivo: los deseados más todo lo que sus 'requires' obliguen.
    closure: dict[str, ComponentDefinition] = {}
    decisions: list[ResolutionDecision] = []
    missing: set[str] = set()
    satisfied: set[str] = set()

    def add_to_closure(component_id: str) -> None:
        if component_id in closure:
            return
        component = registry.get(component_id)
        if component is None:
            return
        closure[component_id] = component
        for capability in component.provides:
            satisfied.add(capability)

        for requirement in component.requires:
            candidates = registry.providers_for(requirement)
            if not candidates:
                missing.add(requirement)
                decisions.append(
                    ResolutionDecision(
                        component_id=component.id,
                        requirement=requirement,
                        candidates=(),
                        reason=f"ninguna definición del catálogo provee '{requirement}'",
                    )
                )
                continue
            candidate_ids = tuple(c.id for c in candidates)
            preferred_id = preferred_components.get((component.id, requirement), "")
            chosen = next((item for item in candidates if item.id == preferred_id), None)
            if preferred_id and chosen is None:
                missing.add(f"{component.id}:{requirement}:invalid-alternative")
                decisions.append(
                    ResolutionDecision(
                        component_id=component.id,
                        requirement=requirement,
                        candidates=candidate_ids,
                        reason=(
                            f"la alternativa elegida '{preferred_id}' ya no provee "
                            f"'{requirement}'"
                        ),
                    )
                )
                continue
            if chosen is None:
                chosen = candidates[0]
                reason = f"'{chosen.id}' es la alternativa recomendada para '{requirement}'"
            else:
                reason = f"alternativa elegida por el usuario: '{chosen.id}'"
            decisions.append(
                ResolutionDecision(
                    component_id=component.id,
                    requirement=requirement,
                    candidates=candidate_ids,
                    chosen_component=chosen.id,
                    reason=reason,
                )
            )
            add_to_closure(chosen.id)

        for optional in component.optional_requires:
            candidates = registry.providers_for(optional)
            if candidates:
                preferred_id = preferred_components.get((component.id, optional), "")
                chosen = next((item for item in candidates if item.id == preferred_id), candidates[0])
                add_to_closure(chosen.id)

    for component_id in desired_component_ids:
        add_to_closure(component_id)

    # 2. Selección de proveedor por componente en el cierre.
    selected_providers: dict[str, str] = {}
    for component_id, component in closure.items():
        provider, reason = _choose_provider(
            component, family, preferred_providers.get(component_id, ""), allowed_provider_types
        )
        if provider is not None:
            selected_providers[component_id] = provider.id
            decisions.append(
                ResolutionDecision(
                    component_id=component_id,
                    requirement="",
                    candidates=tuple(p.id for p in component.providers),
                    chosen_component=component_id,
                    chosen_provider=provider.id,
                    reason=reason,
                )
            )
        elif component.providers:
            # Tenía proveedores pero ninguno compatible: bloqueante si es requerido.
            severity = "bloqueante" if component.criticality == "required" else "opcional, se omite"
            decisions.append(
                ResolutionDecision(
                    component_id=component_id,
                    requirement="",
                    candidates=tuple(p.id for p in component.providers),
                    reason=f"{reason} ({severity})",
                )
            )
            if component.criticality == "required":
                missing.add(f"{component_id}:no-provider")

    return ResolutionResult(
        selected_components=tuple(closure.keys()),
        requested_components=tuple(
            component_id for component_id in dict.fromkeys(desired_component_ids)
            if component_id in closure
        ),
        selected_providers=selected_providers,
        satisfied_capabilities=tuple(sorted(satisfied)),
        missing_capabilities=tuple(sorted(missing)),
        decisions=tuple(decisions),
    )
