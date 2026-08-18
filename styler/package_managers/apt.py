"""Reconciliación semántica de APT/DPKG.

PipeCraft/ProcessRunner sólo cancelan procesos. Decidir si DPKG quedó
inconsistente y cómo reconciliarlo pertenece al dominio del gestor de paquetes.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from styler.execution.processes import ProcessRunner

OutputCallback = Callable[[str], None] | None


@dataclass(frozen=True)
class DpkgReconcileResult:
    needed: bool
    repaired: bool
    message: str
    audit: str = ""
    returncode: int = 0


def is_dpkg_command(argv: Sequence[str]) -> bool:
    names = {Path(value).name for value in argv}
    return bool(names & {"apt", "apt-get", "dpkg"})


def privilege_prefix(argv: Sequence[str]) -> list[str]:
    values = list(argv)
    if values[:2] == ["sudo", "-n"]:
        return ["sudo", "-n"]
    if values[:1] == ["pkexec"]:
        return ["pkexec"]
    return []


def reconcile_dpkg(
    *,
    privilege: Sequence[str] = (),
    on_output: OutputCallback = None,
    runner: ProcessRunner | None = None,
) -> DpkgReconcileResult:
    """Audita DPKG y ejecuta ``dpkg --configure -a`` sólo si hace falta.

    La política de recuperación vive aquí, pero todos los procesos siguen pasando
    por la frontera común ``ProcessRunner``.
    """
    if not shutil.which("dpkg"):
        return DpkgReconcileResult(False, False, "dpkg no está disponible.")

    executor = runner or ProcessRunner(timeout=600)
    audit = executor.run(["dpkg", "--audit"], timeout=20)
    if audit.returncode not in {0, 1}:
        detail = (audit.stderr or audit.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        return DpkgReconcileResult(
            False, False, f"No se pudo comprobar dpkg{suffix}", returncode=audit.returncode
        )

    audit_text = (audit.stdout + "\n" + audit.stderr).strip()
    if not audit_text:
        if on_output is not None:
            on_output("dpkg no reporta paquetes pendientes.")
        return DpkgReconcileResult(False, True, "dpkg quedó consistente.")

    if on_output is not None:
        on_output("dpkg reporta una configuración pendiente; intentando reconciliarla…")
    command = [
        *list(privilege),
        "env",
        "DEBIAN_FRONTEND=noninteractive",
        "DEBCONF_NONINTERACTIVE_SEEN=true",
        "APT_LISTCHANGES_FRONTEND=none",
        "NEEDRESTART_MODE=a",
        "dpkg",
        "--configure",
        "-a",
    ]
    repaired = executor.run(command, timeout=600)

    if repaired.returncode == 0:
        if on_output is not None:
            on_output("dpkg terminó de configurar los paquetes pendientes.")
        return DpkgReconcileResult(True, True, "dpkg fue reparado automáticamente.", audit=audit_text)

    tail = (repaired.stderr or repaired.stdout or "").strip().splitlines()
    reason = tail[-1] if tail else f"código {repaired.returncode}"
    return DpkgReconcileResult(
        True,
        False,
        f"dpkg todavía requiere reparación manual: {reason}",
        audit=audit_text,
        returncode=repaired.returncode,
    )


def reconcile_after_cancel(
    argv: Sequence[str],
    *,
    on_output: OutputCallback = None,
) -> DpkgReconcileResult | None:
    if not is_dpkg_command(argv):
        return None
    return reconcile_dpkg(privilege=privilege_prefix(argv), on_output=on_output)
