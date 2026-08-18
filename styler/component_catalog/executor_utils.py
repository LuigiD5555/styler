"""Utilidades mínimas compartidas por ejecutores de catálogo."""
from __future__ import annotations

from pathlib import Path

from styler.component_catalog.paths import expand_user_path
from styler.execution.processes import ProcessRunner
from styler.planning.models import ExecutionContext, StepDefinition

def _run_probe(
    argv: list[str],
    *,
    timeout: float = 10.0,
    env: dict[str, str] | None = None,
):
    """Sonda sin efectos por la frontera única de procesos de PipeCraft."""
    return ProcessRunner(timeout=timeout).run(argv, timeout=timeout, env=env)


def _home_of(ctx: ExecutionContext) -> Path | None:
    """HOME efectivo. Inyectable por prueba, igual que target/is_root en el resto
    del proyecto: sin esto no se puede probar la escritura real sin tocar el HOME
    de verdad de quien corre las pruebas."""
    injected = ctx.values.get("home")
    return Path(injected) if injected else None


def _target_path(step: StepDefinition, ctx: ExecutionContext) -> Path | None:
    raw = str(step.config.get("target") or step.config.get("backup_source") or "")
    if not raw:
        return None
    return expand_user_path(raw, home=_home_of(ctx))
