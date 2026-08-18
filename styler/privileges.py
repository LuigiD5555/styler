"""Autorización administrativa segura para interfaces de terminal.

El núcleo de Styler nunca lee contraseñas. La TUI suspende temporalmente el
modo de pantalla completa y llama :func:`authorize_sudo_interactive`, de modo
que ``sudo`` recibe el terminal real. Después, todos los comandos privilegiados
usan ``sudo -n`` y comparten la misma credencial temporal.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from styler.execution.processes import ProcessRunner


@dataclass(frozen=True)
class AuthorizationResult:
    ok: bool
    method: str
    message: str
    detail: str = ""
    returncode: int = 0


RunInteractive = Callable[[Sequence[str]], int]


def _run_interactive(argv: Sequence[str]) -> int:
    """Ejecuta heredando el terminal mediante la frontera de PipeCraft."""
    completed = ProcessRunner().run_interactive(list(argv))
    return int(completed.returncode)


def authorize_sudo_interactive(
    *,
    run: RunInteractive | None = None,
    is_root: bool | None = None,
    sudo_path: str | None = None,
) -> AuthorizationResult:
    """Solicita una credencial sudo con el terminal real.

    Esta función no debe ejecutarse mientras Textual controla el terminal. La
    pantalla llamante debe usar ``with app.suspend():``. No se captura ni se
    canaliza la contraseña; ``sudo`` habla directamente con la persona.
    """
    root = os.geteuid() == 0 if is_root is None else is_root
    if root:
        return AuthorizationResult(
            True,
            "root",
            "La sesión ya tiene permisos administrativos.",
        )

    executable = sudo_path or shutil.which("sudo")
    if not executable:
        return AuthorizationResult(
            False,
            "none",
            "Este equipo no tiene sudo disponible.",
            "No se encontró el ejecutable «sudo» en PATH.",
            127,
        )

    runner = run or _run_interactive
    try:
        returncode = runner([executable, "-v"])
    except KeyboardInterrupt:
        return AuthorizationResult(
            False,
            "sudo",
            "La autorización fue cancelada.",
            "La persona interrumpió «sudo -v».",
            130,
        )
    except OSError as exc:
        return AuthorizationResult(
            False,
            "sudo",
            "No se pudo iniciar sudo.",
            str(exc),
            127,
        )

    if returncode == 0:
        return AuthorizationResult(
            True,
            "sudo",
            "Autorización administrativa confirmada.",
        )
    return AuthorizationResult(
        False,
        "sudo",
        "La contraseña fue rechazada o la autorización se canceló.",
        f"«sudo -v» terminó con código {returncode}.",
        returncode,
    )


# --------------------------------------------------------------------------- #
# Mantener viva la autorización durante una instalación larga
# --------------------------------------------------------------------------- #

import threading  # noqa: E402
import time  # noqa: E402


DEFAULT_KEEPALIVE_INTERVAL = 60.0


@dataclass
class SudoTicket:
    """Mantiene vigente la credencial de sudo mientras dura la instalación.

    El problema que resuelve es concreto: el ticket de sudo caduca (por omisión
    a los 15 minutos) y una instalación de KDE Plasma tarda más. Sin esto, la
    persona autoriza una vez, Styler empieza, y a mitad del camino un
    ``sudo -n apt-get install ...`` falla con «permiso rechazado» aunque nadie
    hizo nada mal.

    Refresca con ``sudo -n -v``: **no** lee ni guarda la contraseña; solo extiende
    una autorización que la persona ya concedió. Se detiene siempre al terminar.
    """

    interval: float = DEFAULT_KEEPALIVE_INTERVAL
    run: Callable[[Sequence[str]], int] | None = None
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    refreshes: int = field(default=0, init=False)
    lost: bool = field(default=False, init=False)

    def _refresh_once(self) -> int:
        runner = self.run or _refresh_sudo
        return int(runner(["sudo", "-n", "-v"]))

    def ensure(self) -> bool:
        """Renueva la credencial JUSTO ANTES de un comando privilegiado.

        El temporizador solo no basta: entre dos latidos puede caducar el ticket,
        y entonces un `apt-get install` largo falla con «permiso rechazado» a
        mitad de la instalación. Refrescar antes de cada comando hace que eso sea
        estructuralmente imposible.
        """
        if self._stop.is_set():
            return False
        code = self._refresh_once()
        self.refreshes += 1
        if code != 0:
            self.lost = True
            return False
        return True

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(self.interval):
                code = self._refresh_once()
                self.refreshes += 1
                if code != 0:
                    # La autorización se perdió (p. ej. alguien hizo sudo -k).
                    # No se reintenta a ciegas: el instalador fallará y lo dirá.
                    self.lost = True
                    return

        self._thread = threading.Thread(
            target=loop, name="styler-sudo-keepalive", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def __enter__(self) -> "SudoTicket":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def _refresh_sudo(argv: Sequence[str]) -> int:
    """Refresca sin abrir stdin: si no hay credencial vigente, falla y punto."""
    completed = ProcessRunner(timeout=15).run(
        list(argv), timeout=15, quiet=True
    )
    return int(completed.returncode)


def keepalive_for(prefix: Sequence[str], **kwargs: object) -> Optional[SudoTicket]:
    """Devuelve un mantenedor solo cuando el plan usa sudo (pkexec no lo necesita).

    El intervalo se lee aquí, no en la definición de la clase, para que se pueda
    ajustar (o acortar en pruebas) sin tocar el código.
    """
    if list(prefix[:1]) != ["sudo"]:
        return None
    kwargs.setdefault("interval", DEFAULT_KEEPALIVE_INTERVAL)
    return SudoTicket(**kwargs)  # type: ignore[arg-type]
