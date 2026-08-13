"""Validación semántica del catálogo de componentes.

A diferencia de ``schema.py`` (que rechaza TOML mal formado al cargar),
este módulo no lanza excepciones: produce una lista de ``ValidationIssue``
para que un catálogo con problemas pueda inspeccionarse (``styler catalog
validate``) en vez de abortar en el primer error. La severidad decide si
bloquea (``error``) o solo informa (``warning``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from styler.component_catalog.models import (
    VALID_COMPATIBILITY_VALUES,
    VALID_CRITICALITY,
    VALID_ROLLBACK_LEVELS,
    ComponentDefinition,
)
from styler.component_catalog.registry import ComponentRegistry

# Tipos de proveedor reconocidos. Un tipo fuera de esta lista no puede
# ejecutar comandos arbitrarios: solo operaciones conocidas (sección 26).
KNOWN_PROVIDER_TYPES = (
    "apt",
    "flatpak",
    "snap",
    "pacman",
    "aur",
    "rpm",
    "zypper",
    "archive",
    "appimage",
    "file_overlay",
    "service_enable",
)

# Recursos válidos (sección 27). Namespaces documentados (``user-config:*``,
# ``filesystem:*``) se aceptan aunque el valor exacto no esté listado.
KNOWN_RESOURCE_PREFIXES = ("user-config:", "filesystem:")
KNOWN_RESOURCES = (
    "apt",
    "dpkg",
    "flatpak",
    "snap",
    "network",
    "display-manager",
    "session-manager",
    "package-database",
    "reboot",
    "logout",
)

# Recursos que un tipo de proveedor implica. Si un componente usa un
# proveedor APT pero no declara ``apt``/``dpkg`` como recurso exclusivo,
# dos instalaciones APT podrían pisarse sin que el scheduler lo sepa.
PROVIDER_IMPLIED_RESOURCES: dict[str, tuple[str, ...]] = {
    "apt": ("apt", "dpkg"),
}


@dataclass(frozen=True)
class ValidationIssue:
    severity: str  # error | warning
    code: str
    component_id: str
    path: str
    field: str
    message: str
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "component_id": self.component_id,
            "path": self.path,
            "field": self.field,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


def _issue(
    issues: list[ValidationIssue],
    severity: str,
    code: str,
    component: ComponentDefinition,
    field_name: str,
    message: str,
    suggestion: str = "",
) -> None:
    issues.append(
        ValidationIssue(
            severity=severity,
            code=code,
            component_id=component.id,
            path=component.source.path if component.source else "",
            field=field_name,
            message=message,
            suggestion=suggestion,
        )
    )


def _validate_identity(component: ComponentDefinition, issues: list[ValidationIssue]) -> None:
    if "." not in component.id:
        _issue(
            issues, "warning", "ID_WITHOUT_NAMESPACE", component, "id",
            f"'{component.id}' no usa un namespace (ej. 'app.gimp').",
            "Usa <categoria>.<nombre> para evitar IDs ambiguos como 'gimp' o 'kde'.",
        )
    if component.criticality not in VALID_CRITICALITY:
        _issue(
            issues, "error", "INVALID_CRITICALITY", component, "criticality",
            f"criticidad '{component.criticality}' no es válida.",
            f"Usa uno de: {', '.join(VALID_CRITICALITY)}.",
        )


def _validate_dependencies(
    component: ComponentDefinition, registry: ComponentRegistry, issues: list[ValidationIssue]
) -> None:
    for requirement in component.requires:
        if requirement == component.id:
            _issue(
                issues, "error", "SELF_DEPENDENCY", component, "requires",
                f"'{component.id}' se requiere a sí mismo.",
            )
            continue
        if not registry.providers_for(requirement):
            _issue(
                issues, "error", "MISSING_PROVIDER", component, "requires",
                f"ninguna definición del catálogo provee '{requirement}'.",
                "Agrega un componente que declare esta capacidad en 'provides', "
                "o corrige el nombre de la capacidad.",
            )
    for optional in component.optional_requires:
        if not registry.providers_for(optional):
            _issue(
                issues, "warning", "MISSING_OPTIONAL_PROVIDER", component, "optional_requires",
                f"ninguna definición del catálogo provee la capacidad opcional '{optional}'.",
            )
    for conflict in component.conflicts:
        if conflict == component.id:
            _issue(
                issues, "error", "SELF_CONFLICT", component, "conflicts",
                f"'{component.id}' entra en conflicto consigo mismo.",
            )


def _validate_providers(component: ComponentDefinition, issues: list[ValidationIssue]) -> None:
    if not component.providers and component.kind not in ("application_overlay", "configuration"):
        _issue(
            issues, "warning", "NO_PROVIDERS", component, "providers",
            f"'{component.id}' no declara ningún proveedor de instalación.",
        )
    for provider in component.providers:
        if provider.type not in KNOWN_PROVIDER_TYPES:
            _issue(
                issues, "error", "UNKNOWN_PROVIDER_TYPE", component, f"providers.{provider.id}.type",
                f"tipo de proveedor desconocido: '{provider.type}'.",
                f"Usa uno de: {', '.join(KNOWN_PROVIDER_TYPES)}.",
            )
        if provider.type in ("apt", "pacman", "rpm", "zypper") and not provider.packages:
            _issue(
                issues, "error", "EMPTY_PACKAGE_LIST", component, f"providers.{provider.id}.packages",
                f"el proveedor '{provider.id}' no declara paquetes.",
            )
        if provider.type == "flatpak" and not provider.application_id:
            _issue(
                issues, "error", "MISSING_APPLICATION_ID", component, f"providers.{provider.id}.application_id",
                f"el proveedor '{provider.id}' es flatpak pero no declara 'application_id'.",
            )
        if not provider.families:
            _issue(
                issues, "warning", "NO_FAMILIES", component, f"providers.{provider.id}.families",
                f"el proveedor '{provider.id}' no declara familias compatibles.",
                "Usa ['*'] si aplica a cualquier distribución.",
            )


def _validate_resources(component: ComponentDefinition, issues: list[ValidationIssue]) -> None:
    all_resources = (*component.resources.exclusive, *component.resources.shared)
    for resource in all_resources:
        known = resource in KNOWN_RESOURCES or any(
            resource.startswith(prefix) for prefix in KNOWN_RESOURCE_PREFIXES
        )
        if not known:
            _issue(
                issues, "error", "UNKNOWN_RESOURCE", component, "resources",
                f"recurso desconocido: '{resource}'.",
                "Declara un recurso de la lista conocida o usa un namespace documentado (ej. 'user-config:*').",
            )

    declared_exclusive = set(component.resources.exclusive)
    for provider in component.providers:
        implied = PROVIDER_IMPLIED_RESOURCES.get(provider.type, ())
        missing = [item for item in implied if item not in declared_exclusive]
        if missing:
            _issue(
                issues, "warning", "MISSING_IMPLIED_RESOURCE", component, "resources.exclusive",
                f"el proveedor '{provider.id}' ({provider.type}) implica {missing}, "
                "pero no están en 'resources.exclusive'.",
                f"Agrega {missing} a resources.exclusive para serializar correctamente con otras operaciones {provider.type}.",
            )


def _validate_verification(component: ComponentDefinition, issues: list[ValidationIssue]) -> None:
    if component.criticality == "required" and component.providers and not component.verification.verifiable:
        _issue(
            issues, "error", "MISSING_VERIFICATION", component, "verification",
            f"'{component.id}' es requerido e instalable pero no declara comprobaciones.",
            "Agrega al menos un 'checks' en [verification] (ej. 'executable:<binario>').",
        )


def _validate_rollback(component: ComponentDefinition, issues: list[ValidationIssue]) -> None:
    if component.rollback.level not in VALID_ROLLBACK_LEVELS:
        _issue(
            issues, "error", "INVALID_ROLLBACK_LEVEL", component, "rollback.level",
            f"nivel de rollback '{component.rollback.level}' no es válido.",
            f"Usa uno de: {', '.join(VALID_ROLLBACK_LEVELS)}.",
        )
        return
    if component.rollback.level in ("full", "best_effort") and not component.rollback.strategy:
        _issue(
            issues, "error", "MISSING_ROLLBACK_STRATEGY", component, "rollback.strategy",
            f"nivel de rollback '{component.rollback.level}' sin estrategia declarada.",
        )


def _validate_compatibility(component: ComponentDefinition, issues: list[ValidationIssue]) -> None:
    compat = component.compatibility
    for field_name, value in (
        ("wayland", compat.wayland),
        ("xwayland", compat.xwayland),
        ("x11", compat.x11),
    ):
        if value not in VALID_COMPATIBILITY_VALUES:
            _issue(
                issues, "error", "INVALID_COMPATIBILITY_VALUE", component, f"compatibility.{field_name}",
                f"valor de compatibilidad '{value}' no es válido.",
                f"Usa uno de: {', '.join(VALID_COMPATIBILITY_VALUES)}.",
            )


def _detect_cycles(registry: ComponentRegistry) -> list[list[str]]:
    """Ciclos por dependencia de capacidad (no por proveedor concreto)."""
    components = registry.all()
    graph: dict[str, set[str]] = {c.id: set() for c in components}
    for component in components:
        for requirement in component.requires:
            for provider in registry.providers_for(requirement):
                if provider.id != component.id:
                    graph[component.id].add(provider.id)

    cycles: list[list[str]] = []
    visited: set[str] = set()
    stack: list[str] = []
    on_stack: set[str] = set()

    def visit(node: str) -> None:
        visited.add(node)
        stack.append(node)
        on_stack.add(node)
        for neighbor in sorted(graph.get(node, ())):
            if neighbor not in visited:
                visit(neighbor)
            elif neighbor in on_stack:
                start = stack.index(neighbor)
                cycles.append(stack[start:] + [neighbor])
        stack.pop()
        on_stack.discard(node)

    for node in sorted(graph):
        if node not in visited:
            visit(node)
    return cycles


def validate(registry: ComponentRegistry) -> ValidationReport:
    issues: list[ValidationIssue] = []
    for component in registry.all():
        _validate_identity(component, issues)
        _validate_dependencies(component, registry, issues)
        _validate_providers(component, issues)
        _validate_resources(component, issues)
        _validate_verification(component, issues)
        _validate_rollback(component, issues)
        _validate_compatibility(component, issues)

    for cycle in _detect_cycles(registry):
        issues.append(
            ValidationIssue(
                severity="error",
                code="DEPENDENCY_CYCLE",
                component_id=cycle[0],
                path="",
                field="requires",
                message=f"ciclo de dependencias: {' -> '.join(cycle)}",
                suggestion="Rompe el ciclo eliminando o invirtiendo una de estas dependencias.",
            )
        )

    return ValidationReport(issues)
