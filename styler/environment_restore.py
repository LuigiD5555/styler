"""Planificación del entorno base — LEGADO.

Desde 0.10 este módulo **ya no está en el camino de aplicación**. El escritorio
es un requisito más dentro del plan único de `styler.restore`, que lo resuelve
según la distribución destino (`styler.target`) en vez de exigir el mismo
paquete del equipo original.

Se conserva porque `advanced_restore` (búsqueda de candidatos en repositorios
oficiales, versiones alternativas) sigue siendo útil por separado. No añadas
caminos de instalación nuevos aquí: van en `styler.restore`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from styler import advanced_restore
from styler.component_graph import capabilities_for_package
from styler.dependencies import is_installed
from styler.models import Package


@dataclass(frozen=True)
class EnvironmentInstallStep:
    package: Package
    capability: str
    candidate: advanced_restore.RestoreCandidate

    def to_dict(self) -> dict:
        return {
            "package": {
                "manager": self.package.manager,
                "name": self.package.name,
                "version": self.package.version,
                "architecture": self.package.architecture,
            },
            "capability": self.capability,
            "candidate": self.candidate.to_dict(),
        }


def installation_enabled(root: str = ".") -> bool:
    settings = advanced_restore.load_settings(root)
    return bool(
        settings.enabled
        and settings.allow_repository_lookup
        and settings.allow_installation
    )


def missing_packages(layers: Iterable) -> list[Package]:
    """Devuelve solo entornos base ausentes, nunca temas ni apps opcionales."""
    selected: dict[tuple[str, str], Package] = {}
    for layer in layers:
        for package in layer.packages:
            key = (package.manager.lower(), package.name.lower())
            if key in selected:
                continue
            capabilities = capabilities_for_package(package)
            if not any(capability.startswith("desktop.") for capability in capabilities):
                continue
            if is_installed(package) is False:
                selected[key] = package

    def priority(package: Package) -> tuple[int, str, str]:
        capabilities = capabilities_for_package(package)
        is_desktop = "desktop.kde-plasma" in capabilities
        return (0 if is_desktop else 1, package.manager, package.name.lower())

    return sorted(selected.values(), key=priority)


def packages_to_prepare(layers: Iterable) -> list[Package]:
    """Entornos que deben asegurarse antes de restaurar.

    Plasma se incluye incluso si ya existe: el gestor oficial decidirá si debe
    instalarlo, actualizarlo o dejarlo sin cambios. Otros escritorios solo se
    incluyen cuando están ausentes.
    """
    selected: dict[tuple[str, str], Package] = {}
    for layer in layers:
        for package in layer.packages:
            capabilities = capabilities_for_package(package)
            if not any(capability.startswith("desktop.") for capability in capabilities):
                continue
            kde = "desktop.kde-plasma" in capabilities
            installed = is_installed(package)
            if kde or installed is False:
                selected[(package.manager.lower(), package.name.lower())] = package

    def priority(package: Package) -> tuple[int, str, str]:
        capabilities = capabilities_for_package(package)
        return (0 if "desktop.kde-plasma" in capabilities else 1, package.manager, package.name.lower())

    return sorted(selected.values(), key=priority)


def plan_environment_installation(
    layers: Iterable,
    root: str = ".",
) -> list[EnvironmentInstallStep]:
    settings = advanced_restore.load_settings(root)
    steps: list[EnvironmentInstallStep] = []
    for package in packages_to_prepare(layers):
        capabilities = capabilities_for_package(package)
        capability = capabilities[0] if capabilities else ""
        if capability.startswith("package."):
            candidate = advanced_restore.RestoreCandidate(
                candidate_id=f"environment-{package.manager}-{package.name}",
                capability=capability,
                manager=package.manager,
                name=package.name,
                architecture=package.architecture,
                source_type="repository",
                source="repositorio configurado del sistema",
                relation="available",
                same_provider=True,
                installable=True,
                notes=("Paquete registrado en la configuración original.",),
            )
        else:
            result = advanced_restore.candidates_for_capability(
                capability,
                settings,
                desired_version="",
                preferred_manager=package.manager,
                root=root,
            )
            # Plasma solo puede prepararse desde un repositorio oficial verificado.
            # Se conserva el mismo gestor y paquete cuando existe esa opción; de lo
            # contrario se admite otro proveedor oficial del mismo gestor.
            candidate = next(
                (
                    item for item in result.candidates
                    if item.source_verified
                    and item.manager == package.manager
                    and item.name.lower() == package.name.lower()
                ),
                None,
            )
            if candidate is None:
                candidate = next(
                    (
                        item for item in result.candidates
                        if item.source_verified and item.manager == package.manager
                    ),
                    None,
                )
        if candidate is None:
            raise advanced_restore.AdvancedRestoreError(
                "No se encontró un repositorio oficial verificable para "
                f"{package.manager}:{package.name}. Revisa la guía oficial de KDE y "
                "los repositorios firmados de tu distribución."
            )
        if capability == "desktop.kde-plasma":
            # No fijar la versión capturada: la restauración debe solicitar la
            # versión más reciente que ofrezca el repositorio oficial actual.
            candidate = advanced_restore.replace_candidate_version(candidate, "")
        steps.append(EnvironmentInstallStep(package, capability, candidate))
    return steps


def install_environment(
    layers: Iterable,
    root: str = ".",
    *,
    execute: bool,
    approve: bool,
) -> tuple[list[EnvironmentInstallStep], list[advanced_restore.InstallResult]]:
    settings = advanced_restore.load_settings(root)
    steps = plan_environment_installation(layers, root=root)
    results: list[advanced_restore.InstallResult] = []
    for step in steps:
        result = advanced_restore.install_candidate(
            step.candidate,
            settings,
            execute=execute,
            approve=approve,
            approve_alternative_version=True,
            approve_provider_change=False,
        )
        results.append(result)
        if execute and not result.success:
            break
    return steps, results
