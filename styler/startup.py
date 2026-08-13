"""Arranque seguro cuando Styler fue invocado mediante ``sudo styler``.

La interfaz y los archivos del usuario no deben ejecutarse como root. Si sudo
inició Styler, se recupera la identidad original antes de crear la TUI. La
credencial que sudo acaba de validar permanece disponible para los comandos
posteriores ``sudo -n`` del plan.
"""
from __future__ import annotations

import os
import pwd
from dataclasses import dataclass


@dataclass(frozen=True)
class StartupIdentity:
    changed: bool
    username: str
    uid: int
    gid: int
    message: str = ""


def drop_sudo_root_to_invoking_user() -> StartupIdentity:
    """Abandona root cuando la aplicación fue iniciada con ``sudo styler``.

    No hace nada para root real (sin SUDO_UID), porque no hay una identidad de
    escritorio confiable a la cual regresar. Debe llamarse al principio del
    launcher, antes de crear directorios o importar Textual.
    """
    if os.geteuid() != 0:
        return StartupIdentity(False, "", os.geteuid(), os.getegid())

    raw_uid = os.environ.get("SUDO_UID", "")
    raw_gid = os.environ.get("SUDO_GID", "")
    if not raw_uid.isdigit() or not raw_gid.isdigit():
        return StartupIdentity(
            False,
            "root",
            0,
            os.getegid(),
            "Styler se ejecuta como root real; se recomienda abrirlo como usuario normal.",
        )

    uid = int(raw_uid)
    gid = int(raw_gid)
    account = pwd.getpwuid(uid)

    # Preparar el entorno del usuario antes de perder privilegios.
    os.environ["HOME"] = account.pw_dir
    os.environ["USER"] = account.pw_name
    os.environ["LOGNAME"] = account.pw_name
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")

    try:
        os.initgroups(account.pw_name, gid)
    except OSError:
        # En contenedores o pruebas puede no ser posible; setgid/setuid siguen
        # siendo la frontera esencial.
        os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)

    return StartupIdentity(
        True,
        account.pw_name,
        uid,
        gid,
        "Styler volvió al usuario del escritorio; sudo quedó autorizado para el plan.",
    )
