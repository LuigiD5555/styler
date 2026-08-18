"""
styler.resolution
=================
Cómo se satisface un requisito **en este equipo**.

Dos fuentes, en este orden:

1. **Catálogo** (`styler/catalog/*.toml`): cuando el nombre cambia entre
   distribuciones (`firefox` vs `MozillaFirefox`).
2. **Descubrimiento en vivo**: se le pregunta al gestor del equipo si tiene el
   paquete (`pacman -Si`, `apt-cache policy`, `dnf info`, `flatpak remote-info`).

Gracias al segundo, un perfil capturado en Mint con `apt:krita` se restaura en
Arch como `pacman -S krita` sin que nadie haya escrito esa equivalencia. El
gestor original queda como *preferencia*, no como identidad universal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from styler import catalogs
from styler import resolvers as resolvers_mod
from styler.resolvers import Candidate, Resolver
from styler.execution.processes import Runner
from styler.target import Target


@dataclass(frozen=True)
class Requirement:
    """Lo que una configuración necesita, sin decir cómo conseguirlo."""

    kind: str                     # desktop | manager | application | remote
    key: str
    title: str
    identity: str = ""            # org.kde.krita, org.kde.plasma...
    origin_manager: str = ""      # preferencia original de la persona
    origin_name: str = ""         # nombre del paquete en el equipo original
    version_policy: str = "present"   # present | latest
    mandatory: bool = True
    reproducible: bool = True         # False = AppImage suelto, origen sin confirmar


@dataclass
class Resolution:
    candidate: Optional[Candidate] = None
    reason: str = ""
    unknown_capability: bool = False    # el catálogo no conoce este requisito
    no_manager: bool = False            # ningún gestor de este equipo puede darlo

    @property
    def resolved(self) -> bool:
        return self.candidate is not None


# --------------------------------------------------------------------------- #
# Escritorios y gestores (capacidades del catálogo)
# --------------------------------------------------------------------------- #

def resolve_capability(
    capability: Optional[catalogs.Capability],
    target: Target,
    runner: Runner,
) -> Resolution:
    if capability is None:
        return Resolution(
            unknown_capability=True,
            reason=(
                "Styler no conoce este requisito. Añádelo a un catálogo "
                "(~/.config/styler/catalog/*.toml) con su paquete y cómo verificarlo."
            ),
        )
    if not target.known:
        return Resolution(
            reason=(
                "No se reconoció esta distribución. Añádela a un catálogo de familias "
                "para que Styler sepa qué paquete usar."
            )
        )
    provider = capability.provider_for(target.family)
    if provider is None:
        return Resolution(
            reason=(
                f"El catálogo no dice qué paquete provee «{capability.title}» en "
                f"{target.pretty_name or target.family}."
                + (f" Guía oficial: {capability.guide}" if capability.guide else "")
            )
        )
    resolver = resolvers_mod.resolver_for(provider.manager)
    if resolver is None or not resolver.available(runner):
        return Resolution(
            no_manager=True,
            reason=f"Este equipo no tiene «{provider.manager}» para instalar {capability.title}.",
        )
    return Resolution(
        candidate=Candidate(
            manager=provider.manager,
            package=provider.package,
            reason="catálogo",
            privileged=resolver.privileged,
        ),
        reason=f"{provider.manager}:{provider.package} (catálogo)",
    )


# --------------------------------------------------------------------------- #
# Aplicaciones: identidad portátil + descubrimiento
# --------------------------------------------------------------------------- #

def identity_for(manager: str, name: str, identity: str = "", root: str = ".") -> str:
    """La identidad portátil de una aplicación (AppStream cuando se conoce)."""
    if identity:
        return identity
    catalog = catalogs.cached(root)
    entry = catalog.application_by_name(manager, name)
    if entry is not None:
        return entry.identity
    # Un ID de Flatpak ya ES una identidad AppStream.
    if manager == "flatpak" and "." in name:
        return name
    return ""


def _manager_order(origin_manager: str, target: Target) -> list[str]:
    """Preferencia original → gestor nativo del destino → Flatpak → Snap."""
    order: list[str] = []
    for manager in (origin_manager, target.native_manager, "flatpak", "snap"):
        if manager and manager not in order:
            order.append(manager)
    return order


def _package_name(
    manager: str,
    requirement: Requirement,
    entry: Optional[catalogs.ApplicationEntry],
) -> str:
    if entry is not None:
        name = entry.name_for(manager)
        if name:
            return name
    if manager == requirement.origin_manager:
        return requirement.origin_name
    if manager == "flatpak":
        return requirement.identity if "." in requirement.identity else ""
    # Sin catálogo: se prueba el mismo nombre. Los gestores de Linux coinciden
    # casi siempre («krita», «gimp», «vlc»); si no, `offers()` dirá que no.
    return requirement.origin_name


def resolve_application(
    requirement: Requirement,
    target: Target,
    runner: Runner,
    root: str = ".",
) -> Resolution:
    catalog = catalogs.cached(root)
    identity = identity_for(
        requirement.origin_manager, requirement.origin_name, requirement.identity, root
    )
    entry = catalog.application(identity) if identity else None

    undetermined: Optional[Candidate] = None
    tried: list[str] = []

    for manager in _manager_order(requirement.origin_manager, target):
        resolver: Resolver | None = resolvers_mod.resolver_for(manager)
        if resolver is None or not resolver.available(runner):
            continue
        package = _package_name(manager, requirement, entry)
        if not package:
            continue
        tried.append(f"{manager}:{package}")

        if resolver.installed(package, runner):
            return Resolution(
                candidate=Candidate(manager, package, "ya instalada", resolver.privileged),
                reason=f"{manager}:{package} ya está instalada",
            )

        offers = resolver.offers(package, runner)
        if offers:
            how = "catálogo" if entry and entry.name_for(manager) else "descubierta en el gestor"
            return Resolution(
                candidate=Candidate(manager, package, how, resolver.privileged),
                reason=f"{manager}:{package} ({how})",
            )
        if offers is None and undetermined is None:
            undetermined = Candidate(manager, package, "sin confirmar", resolver.privileged)

    if undetermined is not None and not requirement.reproducible:
        # Origen sin confirmar Y ningún gestor lo confirma: no se promete.
        return Resolution(
            reason=(
                f"«{requirement.title}» no tiene un origen reinstalable confirmado y "
                "ningún gestor de este equipo la ofrece. Instálala a mano."
            )
        )
    if undetermined is not None:
        return Resolution(
            candidate=undetermined,
            reason=(
                f"Ningún gestor confirmó tener «{requirement.title}»; se intentará con "
                f"{undetermined.key}."
            ),
        )
    return Resolution(
        no_manager=True,
        reason=(
            f"Ningún gestor de este equipo ofrece «{requirement.title}». "
            + (f"Se probó: {', '.join(tried)}. " if tried else "")
            + "Instálala a mano o añade su equivalencia a un catálogo."
        ),
    )


# --------------------------------------------------------------------------- #
# Estado e instalación
# --------------------------------------------------------------------------- #

def is_installed(candidate: Candidate, runner: Runner) -> bool | None:
    resolver = resolvers_mod.resolver_for(candidate.manager)
    if resolver is None or not resolver.available(runner):
        return None
    return resolver.installed(candidate.package, runner)


def install_argv(candidate: Candidate, prefix: list[str]) -> list[str]:
    resolver = resolvers_mod.resolver_for(candidate.manager)
    if resolver is None:
        return []
    return resolver.install_argv(candidate.package, prefix if candidate.privileged else [])


def upgrade_argv(candidate: Candidate, prefix: list[str]) -> list[str]:
    resolver = resolvers_mod.resolver_for(candidate.manager)
    if resolver is None:
        return []
    return resolver.upgrade_argv(candidate.package, prefix if candidate.privileged else [])


def refresh_argv(manager: str, prefix: list[str]) -> list[str] | None:
    resolver = resolvers_mod.resolver_for(manager)
    if resolver is None:
        return None
    return resolver.refresh_argv(prefix)


def up_to_date(manager: str, result) -> bool:
    resolver = resolvers_mod.resolver_for(manager)
    return bool(resolver and resolver.up_to_date(result))
