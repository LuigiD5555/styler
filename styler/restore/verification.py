from __future__ import annotations

from ._support import *  # noqa: F401,F403

from .models import *  # noqa: F401,F403

def _verify_item(item: RestoreItem, runner: Runner, root: str):
    if item.kind == "desktop":
        return verify_mod.verify_desktop(item.key.split(":", 1)[1], runner, root)
    if item.kind == "manager":
        return verify_mod.verify_manager(item.key.split(":", 1)[1], runner, root)
    if item.kind == "remote":
        return verify_mod.verify_remote(item.key.split(":", 1)[1], runner, root)
    if item.kind == "application":
        return verify_mod.verify_candidate(
            item.title, item.candidate, runner, mandatory=item.mandatory, key=item.key
        )
    return None

def environment_gate(
    plan: RestorePlan,
    runner: Runner | None = None,
    root: str = ".",
) -> GateResult:
    """Comprueba si el entorno está listo antes de escribir personalización."""
    runner = runner or ProcessRunner()
    result = verify_mod.verify_requirements(
        environment_id=next(
            (item.key.split(":", 1)[1] for item in plan.items if item.kind == "desktop"), ""
        ),
        managers=[item.key.split(":", 1)[1] for item in plan.items if item.kind == "manager"],
        remotes=[item.key.split(":", 1)[1] for item in plan.items if item.kind == "remote"],
        candidates=[
            (item.key, item.title, item.candidate, item.mandatory)
            for item in plan.items
            if item.kind == "application" and item.status != ItemStatus.SKIPPED_BY_USER
        ],
        runner=runner,
        root=root,
    )
    if result.ok:
        return GateResult(True, verification=result)
    missing = ", ".join(check.title for check in result.failures())
    return GateResult(
        False,
        (
            f"El entorno todavía no está completo: {missing}. "
            "Ejecuta primero el pipeline de entorno; Styler no copiará archivos "
            "hasta que sus requisitos estén instalados y verificados."
        ),
        result,
    )
