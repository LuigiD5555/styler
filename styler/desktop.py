"""Acciones de sesión solicitadas explícitamente por la persona usuaria."""
from __future__ import annotations

import shutil
from dataclasses import dataclass

from styler.compat import detect_environment
from styler.runtime.commands import PipeCraftRunner


@dataclass(frozen=True)
class ReloadResult:
    ok: bool
    message: str
    details: tuple[str, ...] = ()


def _call(command: list[str], timeout: int = 20) -> tuple[bool, str]:
    result = PipeCraftRunner(timeout=timeout).run(command, timeout=timeout)
    detail = (result.stderr or result.stdout).strip()
    return result.returncode == 0, detail


def reload_plasma() -> ReloadResult:
    """Recarga Plasma solo tras una acción explícita desde la interfaz.

    Primero intenta servicios de usuario de Plasma 6/5. Si no están
    disponibles, reconfigura KWin. Nunca usa sudo ni mata toda la sesión.
    """
    environment = detect_environment()
    if not environment.is_kde():
        return ReloadResult(False, "Esta sesión no parece ser KDE Plasma.")

    attempts: list[str] = []
    if shutil.which("systemctl"):
        for service in ("plasma-plasmashell.service", "plasma-plasmashell"):
            ok, detail = _call(["systemctl", "--user", "restart", service])
            attempts.append(f"systemctl --user restart {service}: {detail or ('ok' if ok else 'no disponible')}")
            if ok:
                return ReloadResult(
                    True,
                    "Plasma se recargó. Algunas aplicaciones abiertas pueden necesitar reiniciarse.",
                    tuple(attempts),
                )

    for command in ("qdbus6", "qdbus"):
        if not shutil.which(command):
            continue
        ok, detail = _call([command, "org.kde.KWin", "/KWin", "reconfigure"])
        attempts.append(f"{command} KWin reconfigure: {detail or ('ok' if ok else 'falló')}")
        if ok:
            return ReloadResult(
                True,
                "KWin volvió a leer parte de la configuración. Para paneles o widgets, cierra sesión y vuelve a entrar.",
                tuple(attempts),
            )

    return ReloadResult(
        False,
        "No fue posible recargar Plasma automáticamente. Cierra sesión y vuelve a entrar para ver todos los cambios.",
        tuple(attempts),
    )
