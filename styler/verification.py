"""
styler.verification
===================
Que un comando termine en 0 no significa que el entorno exista.

Tres reglas que no se negocian:

* **Desconocido no es presente.** Si Styler no sabe cómo comprobar un
  escritorio, no lo da por bueno: lo declara sin resolver y se detiene.
* **Indeterminado bloquea.** Si el gestor no puede confirmar una aplicación
  obligatoria, eso NO es un éxito. Solo lo opcional puede seguir sin confirmar.
* Nada de esto adivina: cada comprobación mira el sistema.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from styler import catalogs
from styler import resolution as resolution_mod
from styler import target as target_mod
from styler.applications import AppSpec
from styler.resolvers import Candidate
from styler.execution.processes import Runner, ProcessRunner


@dataclass
class Check:
    key: str
    title: str
    ok: bool
    detail: str = ""
    mandatory: bool = True
    indeterminate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "ok": self.ok,
            "detail": self.detail,
            "mandatory": self.mandatory,
            "indeterminado": self.indeterminate,
        }


@dataclass
class VerificationResult:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if check.mandatory)

    def failures(self) -> list[Check]:
        return [check for check in self.checks if check.mandatory and not check.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [check.to_dict() for check in self.checks],
            "fallidas": [check.title for check in self.failures()],
        }


def verify_desktop(environment_id: str, runner: Runner, root: str = ".") -> Check:
    """Un escritorio que no se puede comprobar NO se considera presente."""
    capability = catalogs.cached(root).desktop(environment_id)
    key = f"desktop:{environment_id}"

    if capability is None:
        return Check(
            key=key,
            title=f"Escritorio {environment_id}",
            ok=False,
            indeterminate=True,
            detail=(
                f"Styler no conoce «{environment_id}». Desconocido no significa presente: "
                "añade un catálogo con su paquete y su ejecutable de verificación."
            ),
        )
    if not capability.verifiable:
        return Check(
            key=key,
            title=capability.title,
            ok=False,
            indeterminate=True,
            detail=(
                f"El catálogo no dice cómo comprobar «{capability.title}». "
                "Sin verificación, Styler no copia configuraciones que dependen de él."
            ),
        )

    found = [name for name in capability.verify_executables if runner.available(name)]
    return Check(
        key=key,
        title=capability.title,
        ok=bool(found),
        detail=(
            f"Encontrado: {', '.join(found)}."
            if found
            else f"No se encontró {', '.join(capability.verify_executables)}."
        ),
    )


def verify_manager(manager: str, runner: Runner, root: str = ".") -> Check:
    program = target_mod.manager_binary(manager, root)
    ok = runner.available(program)
    return Check(
        key=f"manager:{manager}",
        title=f"Gestor {manager}",
        ok=ok,
        detail="Disponible." if ok else f"«{program}» no está disponible en este equipo.",
    )


def verify_remote(remote: str, runner: Runner, root: str = ".") -> Check:
    remotes = target_mod.configured_flatpak_remotes(runner)
    ok = remote.lower() in remotes
    return Check(
        key=f"remote:{remote}",
        title=f"Remoto {remote}",
        ok=ok,
        detail="Configurado." if ok else "No aparece entre los remotos de Flatpak.",
    )


def verify_candidate(
    title: str,
    candidate: Optional[Candidate],
    runner: Runner,
    mandatory: bool = True,
    key: str = "",
) -> Check:
    """Regla: True → satisfecho · False → fallido · None → INDETERMINADO y bloqueante."""
    if candidate is None:
        return Check(
            key=key or f"app:{title}",
            title=title,
            ok=False,
            mandatory=mandatory,
            indeterminate=True,
            detail="No se resolvió ningún gestor capaz de proveerla.",
        )
    present = resolution_mod.is_installed(candidate, runner)
    if present is None:
        return Check(
            key=key or f"app:{candidate.key}",
            title=title,
            ok=False,
            mandatory=mandatory,
            indeterminate=True,
            detail=(
                f"El gestor «{candidate.manager}» no pudo confirmar «{candidate.package}». "
                "Una verificación indeterminada no es un éxito."
            ),
        )
    return Check(
        key=key or f"app:{candidate.key}",
        title=title,
        ok=bool(present),
        mandatory=mandatory,
        detail="Instalada." if present else "No aparece instalada.",
    )


def verify_application(
    spec: AppSpec,
    runner: Runner,
    mandatory: bool = True,
    target=None,
    root: str = ".",
) -> Check:
    from styler.applications import _requirement

    target = target or target_mod.detect_target(root=root)
    result = resolution_mod.resolve_application(_requirement(spec, root), target, runner, root)
    return verify_candidate(
        spec.title, result.candidate, runner, mandatory=mandatory, key=f"app:{spec.app_id}"
    )


def verify_requirements(
    environment_id: str = "",
    managers: list[str] | None = None,
    remotes: list[str] | None = None,
    candidates: list[tuple[str, str, Optional[Candidate], bool]] | None = None,
    runner: Runner | None = None,
    root: str = ".",
) -> VerificationResult:
    runner = runner or ProcessRunner()
    result = VerificationResult()
    if environment_id:
        result.checks.append(verify_desktop(environment_id, runner, root))
    for manager in managers or []:
        result.checks.append(verify_manager(manager, runner, root))
    for remote in remotes or []:
        result.checks.append(verify_remote(remote, runner, root))
    for key, title, candidate, mandatory in candidates or []:
        result.checks.append(
            verify_candidate(title, candidate, runner, mandatory=mandatory, key=key)
        )
    return result
