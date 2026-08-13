"""Modelo semántico de componentes y relaciones de Styler.

Esta capa complementa el grafo técnico del motor. Los gestores resuelven sus
bibliotecas internas; Styler resuelve relaciones que esos gestores no conocen:
una personalización que requiere una aplicación, una extensión incompatible o
un recurso opcional.

El módulo es deliberadamente de solo planificación. No instala paquetes ni
copia archivos.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from styler.models import Component, FileEntry, Package
from styler.parts import title_for

if TYPE_CHECKING:  # pragma: no cover
    from styler.layers import Layer
    from styler.provenance.models import Inventory


class ComponentType:
    DESKTOP_ENVIRONMENT = "desktop-environment"
    APPLICATION = "application"
    APPLICATION_OVERLAY = "application-overlay"
    DESKTOP_CONFIGURATION = "desktop-configuration"
    RESOURCE = "resource"
    SERVICE = "service"
    GENERIC = "generic"


class RelationType:
    REQUIRES = "requires"
    OPTIONAL = "optional"
    CONFLICTS = "conflicts"
    REPLACES = "replaces"


@dataclass(frozen=True)
class ProviderVariant:
    """Cómo una capacidad puede estar instalada en un gestor concreto."""

    capability: str
    manager: str
    package_name: str
    config_root: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "capability": self.capability,
            "manager": self.manager,
            "package_name": self.package_name,
            "config_root": self.config_root,
        }


@dataclass(frozen=True)
class ComponentIssue:
    severity: str  # error | warning | info
    code: str
    component_id: str
    message: str
    related: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "component_id": self.component_id,
            "message": self.message,
            "related": list(self.related),
        }


@dataclass
class ComponentPlan:
    components: list[Component]
    order: list[str]
    issues: list[ComponentIssue] = field(default_factory=list)
    selected_providers: dict[str, str] = field(default_factory=dict)
    selected_variants: dict[str, str] = field(default_factory=dict)

    @property
    def blocking_issues(self) -> list[ComponentIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def ready(self) -> bool:
        return not self.blocking_issues

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "order": list(self.order),
            "selected_providers": dict(self.selected_providers),
            "selected_variants": dict(self.selected_variants),
            "issues": [issue.to_dict() for issue in self.issues],
            "components": [component.to_dict() for component in self.components],
        }


# Una capacidad permanece estable aunque cambie el nombre del paquete entre
# distribuciones. El catálogo TOML es la única fuente de verdad; este módulo
# solo proyecta sus proveedores al modelo de impacto usado por la interfaz.

def _providers_from_catalog() -> dict[str, tuple[ProviderVariant, ...]]:
    """Traduce el catálogo declarativo a variantes de proveedor.

    Un catálogo inválido debe fallar de forma visible: mantener una segunda
    tabla de respaldo ocultaría divergencias y devolvería planes distintos
    según el camino de código utilizado.
    """
    from styler.component_catalog.loader import load

    report = load(root=".")
    result: dict[str, tuple[ProviderVariant, ...]] = {}
    for loaded in report.components.values():
        component = loaded.definition
        capability = component.capability_alias
        if not capability:
            continue
        variants: list[ProviderVariant] = []
        for provider in sorted(component.providers, key=lambda p: (-p.priority, p.id)):
            names = list(provider.packages)
            if provider.type == "flatpak" and provider.application_id:
                names = [provider.application_id]
            for name in names:
                if name:
                    variants.append(
                        ProviderVariant(
                            capability=capability,
                            manager=provider.type,
                            package_name=name,
                            config_root=provider.config_root,
                        )
                    )
        if variants:
            result[capability] = tuple(variants)
    return result


CAPABILITY_PROVIDERS: dict[str, tuple[ProviderVariant, ...]] = _providers_from_catalog()


PART_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "gimp": ("application.gimp",),
    "konsole": ("application.konsole",),
    "dolphin": ("application.dolphin",),
    "tema-colores": ("desktop.kde-plasma",),
    "paneles": ("desktop.kde-plasma",),
    "atajos": ("desktop.kde-plasma",),
}

PART_TYPES: dict[str, str] = {
    "gimp": ComponentType.APPLICATION_OVERLAY,
    "konsole": ComponentType.APPLICATION_OVERLAY,
    "dolphin": ComponentType.APPLICATION_OVERLAY,
    "tema-colores": ComponentType.DESKTOP_CONFIGURATION,
    "paneles": ComponentType.DESKTOP_CONFIGURATION,
    "atajos": ComponentType.DESKTOP_CONFIGURATION,
    "iconos": ComponentType.RESOURCE,
    "cursores": ComponentType.RESOURCE,
    "fuentes": ComponentType.RESOURCE,
    "fondos": ComponentType.RESOURCE,
}


_PACKAGE_INDEX: dict[tuple[str, str], ProviderVariant] = {}
for _variants in CAPABILITY_PROVIDERS.values():
    for _variant in _variants:
        _PACKAGE_INDEX[(_variant.manager.lower(), _variant.package_name.lower())] = _variant


def capabilities_for_package(package: Package) -> list[str]:
    variant = _PACKAGE_INDEX.get((package.manager.lower(), package.name.lower()))
    if variant:
        return [variant.capability]
    # Capacidad estable de último recurso. No se usa para inferir compatibilidad,
    # pero permite relacionar exactamente el mismo paquete en una receta.
    normalized = package.name.lower().replace("_", "-")
    return [f"package.{package.manager.lower()}.{normalized}"]


def variant_for(package: Package, capability: str = "") -> ProviderVariant | None:
    variant = _PACKAGE_INDEX.get((package.manager.lower(), package.name.lower()))
    if variant and (not capability or capability == variant.capability):
        return variant
    return None


def component_from_package(package: Package) -> Component:
    component_id = _safe_id(f"app-{package.manager}-{package.name}")
    capabilities = capabilities_for_package(package)
    return Component(
        component_id=component_id,
        title=f"{package.name} ({package.manager})",
        category="aplicaciones",
        component_type=ComponentType.APPLICATION,
        provides=capabilities,
        packages=[package],
        verification=[
            {
                "type": "package-present",
                "manager": package.manager,
                "name": package.name,
                "version": package.version,
            }
        ],
        human_summary=f"Aplicación provista por {package.manager}: {package.name}.",
    )


def component_from_layer(layer: "Layer") -> Component:
    part_id = layer.part_id
    title = layer.title or title_for(part_id)
    component_id = _safe_id(f"layer-{layer.layer_id}")
    requires = list(PART_REQUIREMENTS.get(part_id, ()))
    component_type = PART_TYPES.get(part_id, ComponentType.GENERIC)

    if part_id == "gimp":
        # Una capa bajo ~/.config/GIMP es una personalización sobre GIMP. Si la
        # capa se describe como PhotoGIMP, se conserva ese nombre humano.
        hay_photogimp = "photogimp" in (title + " " + layer.notes).lower()
        if hay_photogimp:
            title = "PhotoGIMP"
        else:
            title = title or "Personalización de GIMP"

    verification = [
        {"type": "file-present", "path": entry.path, "checksum": entry.checksum}
        for entry in layer.files[:200]
    ]
    provider_variants: dict[str, dict[str, str]] = {}
    for capability in requires:
        for candidate in CAPABILITY_PROVIDERS.get(capability, ()):
            provider_variants[candidate.manager] = {
                "package_name": candidate.package_name,
                "config_root": candidate.config_root,
            }

    return Component(
        component_id=component_id,
        title=title,
        category="aplicaciones" if component_type == ComponentType.APPLICATION_OVERLAY else "configuracion",
        component_type=component_type,
        requires=requires,
        packages=[],
        files=list(layer.files),
        provider_variants=provider_variants,
        verification=verification,
        human_summary=(
            f"{title} necesita {', '.join(requires)}."
            if requires else f"Parte reutilizable: {title}."
        ),
    )



def component_from_desktop_environment(environment) -> Component:
    package = environment.package()
    packages = [package] if package is not None else []
    verification = []
    if package is not None:
        verification.append({
            "type": "package-present",
            "manager": package.manager,
            "name": package.name,
            "version": package.version,
        })
    return Component(
        component_id=f"environment-{environment.environment_id}",
        title=environment.name or "Entorno de escritorio",
        category="entorno-base",
        component_type=ComponentType.DESKTOP_ENVIRONMENT,
        provides=[f"desktop.{environment.environment_id}"],
        packages=packages,
        verification=verification,
        source={
            "official_project_url": environment.official_project_url,
            "official_install_url": environment.official_install_url,
            "detected_by": list(environment.detected_by),
        },
        human_summary=(
            f"Entorno base observado: {environment.name}. "
            "Se reinstala antes de aplicar paneles, atajos o preferencias."
        ),
    )

def components_from_layers(
    layers: Iterable["Layer"],
    inventory: "Inventory | None" = None,
) -> list[Component]:
    """Convierte capas y aplicaciones observadas en componentes semánticos."""
    layers = list(layers)
    components: list[Component] = []
    package_keys: set[tuple[str, str]] = set()
    environment_ids: set[str] = set()

    for layer in layers:
        for environment in getattr(layer, "desktop_environments", []):
            if not environment.environment_id or environment.environment_id in environment_ids:
                continue
            environment_ids.add(environment.environment_id)
            component = component_from_desktop_environment(environment)
            components.append(component)
            for package in component.packages:
                package_keys.add((package.manager.lower(), package.name.lower()))

    for layer in layers:
        for package in layer.packages:
            key = (package.manager.lower(), package.name.lower())
            if key not in package_keys:
                package_keys.add(key)
                components.append(component_from_package(package))
        components.append(component_from_layer(layer))

    # El inventario permite satisfacer dependencias aunque el paquete no haya
    # quedado asociado a una capa por la inferencia conservadora anterior.
    if inventory is not None:
        needed = {
            capability
            for component in components
            for capability in component.requires
        }
        already = {
            capability
            for component in components
            for capability in component.provides
        }
        for record in inventory.applications:
            package = Package(
                manager=record.manager,
                name=record.name,
                version=record.version,
                architecture=record.architecture,
            )
            capabilities = capabilities_for_package(package)
            if not (set(capabilities) & (needed - already)):
                continue
            key = (package.manager.lower(), package.name.lower())
            if key in package_keys:
                continue
            package_keys.add(key)
            components.append(component_from_package(package))
            already.update(capabilities)

    return components


def enrich_component(component: Component) -> Component:
    """Añade semántica conocida a componentes antiguos sin borrar metadatos."""
    if component.packages:
        component.component_type = component.component_type or ComponentType.APPLICATION
        for package in component.packages:
            for capability in capabilities_for_package(package):
                if capability not in component.provides:
                    component.provides.append(capability)
        if not component.verification:
            component.verification = [
                {
                    "type": "package-present",
                    "manager": package.manager,
                    "name": package.name,
                    "version": package.version,
                }
                for package in component.packages
            ]

    paths = [entry.path for entry in component.files]
    if any("/.config/GIMP" in path or "/org.gimp.GIMP/" in path for path in paths):
        component.component_type = ComponentType.APPLICATION_OVERLAY
        if "application.gimp" not in component.requires:
            component.requires.append("application.gimp")
        if "PhotoGIMP" not in component.title and "photogimp" in component.human_summary.lower():
            component.title = "PhotoGIMP"
    elif any("konsolerc" in path or "/share/konsole" in path for path in paths):
        component.component_type = ComponentType.APPLICATION_OVERLAY
        if "application.konsole" not in component.requires:
            component.requires.append("application.konsole")
    elif any("dolphinrc" in path or "/share/dolphin" in path for path in paths):
        component.component_type = ComponentType.APPLICATION_OVERLAY
        if "application.dolphin" not in component.requires:
            component.requires.append("application.dolphin")

    if component.files and not component.verification:
        component.verification = [
            {"type": "file-present", "path": entry.path, "checksum": entry.checksum}
            for entry in component.files[:200]
        ]
    return component


def resolve_component_graph(components: Iterable[Component]) -> ComponentPlan:
    """Resuelve capacidades, detecta faltantes/conflictos y ordena el grafo."""
    items = [enrich_component(component) for component in components]
    by_id = {component.component_id: component for component in items}
    providers: dict[str, list[Component]] = {}
    for component in items:
        for capability in component.provides:
            providers.setdefault(capability, []).append(component)

    issues: list[ComponentIssue] = []
    selected: dict[str, str] = {}
    variants: dict[str, str] = {}

    for component in items:
        for requirement in component.requires:
            candidates = providers.get(requirement, [])
            if not candidates:
                environment_requirement = requirement.startswith("desktop.")
                issues.append(
                    ComponentIssue(
                        "warning" if environment_requirement else "error",
                        (
                            "MISSING_ENVIRONMENT_CAPABILITY"
                            if environment_requirement
                            else "MISSING_REQUIRED_CAPABILITY"
                        ),
                        component.component_id,
                        f"{component.title} necesita {requirement}, pero no hay proveedor disponible.",
                        (requirement,),
                    )
                )
                continue
            provider = _select_provider(candidates)
            selected[f"{component.component_id}:{requirement}"] = provider.component_id
            if provider.component_id != component.component_id and provider.component_id not in component.depends_on:
                component.depends_on.append(provider.component_id)
            package = provider.packages[0] if provider.packages else None
            if package:
                candidate = variant_for(package, requirement)
                if candidate:
                    variants[f"{component.component_id}:{requirement}"] = candidate.manager
                    if candidate.config_root:
                        component.selected_provider = provider.component_id
                        component.selected_variant = candidate.manager
                        component.target_root = candidate.config_root

        for optional in component.optional:
            if not providers.get(optional):
                issues.append(
                    ComponentIssue(
                        "warning",
                        "MISSING_OPTIONAL_CAPABILITY",
                        component.component_id,
                        f"{component.title} puede funcionar sin {optional}, pero quedará incompleto.",
                        (optional,),
                    )
                )

        for conflict in component.conflicts:
            conflicting = providers.get(conflict, [])
            if conflicting:
                issues.append(
                    ComponentIssue(
                        "error",
                        "CONFLICTING_CAPABILITY",
                        component.component_id,
                        f"{component.title} entra en conflicto con {conflict}.",
                        tuple(item.component_id for item in conflicting),
                    )
                )

    order, cycle = _topological_components(items)
    if cycle:
        for component_id in cycle:
            issues.append(
                ComponentIssue(
                    "error",
                    "DEPENDENCY_CYCLE",
                    component_id,
                    "La relación entre componentes forma un ciclo y no puede ordenarse.",
                    tuple(cycle),
                )
            )

    # Dependencias directas antiguas que apuntan a un componente ausente.
    for component in items:
        for dependency in component.depends_on:
            if dependency not in by_id:
                issues.append(
                    ComponentIssue(
                        "error",
                        "MISSING_COMPONENT",
                        component.component_id,
                        f"Falta el componente requerido {dependency}.",
                        (dependency,),
                    )
                )

    return ComponentPlan(items, order, issues, selected, variants)


def _select_provider(candidates: list[Component]) -> Component:
    # Preferimos proveedores ya explícitos en la configuración. El orden es
    # estable y no supone que un gestor sea universalmente mejor que otro.
    return sorted(candidates, key=lambda item: item.component_id)[0]


def _topological_components(components: list[Component]) -> tuple[list[str], list[str]]:
    ids = [component.component_id for component in components]
    known = set(ids)
    indegree = {component_id: 0 for component_id in ids}
    outgoing: dict[str, list[str]] = {component_id: [] for component_id in ids}
    for component in components:
        for dependency in dict.fromkeys(component.depends_on):
            if dependency not in known:
                continue
            indegree[component.component_id] += 1
            outgoing[dependency].append(component.component_id)

    ready = [component_id for component_id in ids if indegree[component_id] == 0]
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for follower in outgoing[current]:
            indegree[follower] -= 1
            if indegree[follower] == 0:
                ready.append(follower)

    cycle = [component_id for component_id in ids if indegree[component_id] > 0]
    return order, cycle


def _safe_id(value: str) -> str:
    result = "".join(char if char.isalnum() or char in "._-" else "-" for char in value)
    result = result.strip("-._") or "component"
    return result[:128]

@dataclass(frozen=True)
class ComponentImpact:
    """Resultado legible de retirar o sustituir un componente del plan."""

    component_id: str
    affected: tuple[str, ...]
    independent: tuple[str, ...]
    missing_capabilities: tuple[str, ...] = ()

    @property
    def safe(self) -> bool:
        return not self.missing_capabilities and self.affected == (self.component_id,)


def component_impact(
    plan: ComponentPlan,
    component_id: str,
    replacement_provides: Iterable[str] = (),
) -> ComponentImpact:
    """Calcula qué rama se rompe al retirar o sustituir un componente.

    ``replacement_provides`` permite simular un cambio de proveedor. Si la
    sustitución conserva las mismas capacidades, los consumidores continúan
    siendo válidos y no se marcan como afectados.
    """
    by_id = {component.component_id: component for component in plan.components}
    if component_id not in by_id:
        raise KeyError(component_id)

    replacement = set(replacement_provides)
    removed = {component_id}
    affected = {component_id}
    missing: set[str] = set()

    providers: dict[str, set[str]] = {}
    for component in plan.components:
        if component.component_id in removed:
            continue
        for capability in component.provides:
            providers.setdefault(capability, set()).add(component.component_id)
    for capability in replacement:
        providers.setdefault(capability, set()).add("__replacement__")

    changed = True
    while changed:
        changed = False
        for component in plan.components:
            if component.component_id in affected:
                continue
            if any(dependency in affected for dependency in component.depends_on):
                # Una arista puede quedar satisfecha por una sustitución que
                # conserve todas las capacidades que el consumidor requiere.
                unavailable = [
                    requirement for requirement in component.requires
                    if not providers.get(requirement)
                ]
                if not unavailable:
                    continue
                affected.add(component.component_id)
                missing.update(unavailable)
                changed = True
                continue
            unavailable = [
                requirement for requirement in component.requires
                if not providers.get(requirement)
            ]
            if unavailable:
                affected.add(component.component_id)
                missing.update(unavailable)
                changed = True

    ordered_affected = tuple(
        component.component_id for component in plan.components
        if component.component_id in affected
    )
    independent = tuple(
        component.component_id for component in plan.components
        if component.component_id not in affected
    )
    return ComponentImpact(
        component_id=component_id,
        affected=ordered_affected,
        independent=independent,
        missing_capabilities=tuple(sorted(missing)),
    )
