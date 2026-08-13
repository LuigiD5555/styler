"""Instalación desatendida y dos pipelines separados por una compuerta.

Lo que estas pruebas defienden:

* La persona pulsa Aceptar **una vez** y no vuelve a intervenir.
* La autorización sigue viva aunque KDE Plasma tarde más que el ticket de sudo.
* El pipeline de entorno **nunca** toca el HOME.
* El de personalización **nunca** corre si el entorno no está verificado.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from styler import pipelines
from styler import privileges
from styler.applications import AppSpec
from styler.layers import Layer, save_layer
from styler.models import DesktopEnvironmentRecord, FileEntry
from styler.objectstore import ObjectStore
from styler.profiles import create_profile, save_profile
from styler.restore import ItemStatus
from styler.runtime.commands import FakeRunner
from styler.target import Target

MINT = Target(distro_id="linuxmint", family="ubuntu", pretty_name="Linux Mint 22.3")


def _profile(root: Path) -> str:
    store = ObjectStore(root=str(root))
    entries = []
    for name, path in (
        ("kdeglobals", "${HOME}/.config/kdeglobals"),
        ("appletsrc", "${HOME}/.config/plasma-org.kde.plasma.desktop-appletsrc"),
    ):
        source = root / name
        source.write_text(name)
        checksum, _ = store.store_file(source)
        entries.append(FileEntry(path=path, checksum=checksum, size=len(name)))
    layer = Layer(
        layer_id="l1",
        part_id="tema-colores",
        title="Escritorio",
        desktop="kde-plasma",
        desktop_environments=[
            DesktopEnvironmentRecord(environment_id="kde-plasma", name="KDE Plasma")
        ],
        files=entries,
        applications=[AppSpec(manager="apt", name="konsole", display_name="Konsole")],
    )
    save_layer(layer, root=str(root))
    profile = create_profile("Mi Plasma", [layer.layer_id])
    save_profile(profile, root=str(root))
    return profile.profile_id


def _mint(**kwargs) -> FakeRunner:
    return FakeRunner(
        programs={"apt-get", "apt-cache", "dpkg-query", "sudo"},
        provides={"apt:kde-plasma-desktop": ("plasmashell",)},
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# La autorización sobrevive a una instalación larga
# --------------------------------------------------------------------------- #

def test_the_sudo_ticket_is_kept_alive_during_a_long_installation():
    """KDE Plasma tarda más de 15 minutos; el ticket de sudo caduca antes."""
    calls: list[list[str]] = []
    ticket = privileges.SudoTicket(
        interval=0.01, run=lambda argv: (calls.append(list(argv)), 0)[1]
    )
    with ticket:
        time.sleep(0.06)

    assert ticket.refreshes >= 2
    assert calls[0] == ["sudo", "-n", "-v"]      # refresca, nunca pide contraseña
    assert ticket.lost is False


def test_a_lost_authorization_is_reported_not_silently_retried():
    ticket = privileges.SudoTicket(interval=0.01, run=lambda _argv: 1)
    with ticket:
        time.sleep(0.05)
    assert ticket.lost is True


def test_pkexec_does_not_need_a_keepalive():
    assert privileges.keepalive_for(["pkexec"]) is None
    assert privileges.keepalive_for([]) is None          # ya somos root
    assert privileges.keepalive_for(["sudo", "-n"]) is not None


def test_one_approval_installs_the_whole_desktop_without_asking_again(tmp_path: Path):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _profile(root)
    runner = _mint()

    report = pipelines.run(
        pipelines.ALL, "profile", profile_id,
        root=str(root), home=home, execute=True, approve=True,
        runner=runner, target=MINT, is_root=False,
    )

    assert report.ok is True
    assert report.environment_ready is True
    assert report.files_applied is True
    # Ni una sola petición interactiva de contraseña: todo va con «sudo -n».
    privileged = [call for call in runner.calls if call[:1] == ["sudo"]]
    assert privileged, "la instalación debía usar privilegios"
    assert all(call[:2] == ["sudo", "-n"] for call in privileged)
    # Y APT nunca abre un diálogo que nadie pueda contestar.
    apt = next(call for call in runner.calls if "install" in call and "kde-plasma-desktop" in call)
    assert "DEBIAN_FRONTEND=noninteractive" in apt


# --------------------------------------------------------------------------- #
# Dos pipelines, una compuerta
# --------------------------------------------------------------------------- #

def test_the_environment_pipeline_never_touches_the_home(tmp_path: Path):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _profile(root)
    runner = _mint()

    report = pipelines.run(
        pipelines.ENVIRONMENT, "profile", profile_id,
        root=str(root), home=home, execute=True, approve=True,
        runner=runner, target=MINT, is_root=True,
    )

    assert report.ok is True                 # instalar el sistema ya es un éxito
    assert report.environment_ready is True
    assert report.files_applied is False
    assert list(home.rglob("*")) == []       # el HOME quedó intacto
    desktop = next(item for item in report.plan.items if item.kind == "desktop")
    assert desktop.status == ItemStatus.INSTALLED


def test_the_personalization_pipeline_refuses_to_run_without_a_verified_environment(
    tmp_path: Path,
):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _profile(root)
    runner = _mint()                      # Plasma NO está instalado

    report = pipelines.run(
        pipelines.PERSONALIZATION, "profile", profile_id,
        root=str(root), home=home, execute=True, approve=True,
        runner=runner, target=MINT, is_root=True,
    )

    assert report.ok is False
    assert report.files_applied is False
    assert list(home.rglob("*")) == []
    assert "pipeline de entorno" in report.aborted_reason
    # Y no intentó instalar nada: este pipeline no instala, comprueba.
    assert not any("install" in call for call in runner.calls)


def test_the_two_pipelines_chain_when_run_one_after_the_other(tmp_path: Path):
    """Preparar la máquina hoy, traerte tu configuración mañana."""
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _profile(root)
    runner = _mint()

    first = pipelines.run(
        pipelines.ENVIRONMENT, "profile", profile_id,
        root=str(root), home=home, execute=True, approve=True,
        runner=runner, target=MINT, is_root=True,
    )
    assert first.environment_ready is True
    assert list(home.rglob("*")) == []

    mark = len(runner.calls)
    second = pipelines.run(
        pipelines.PERSONALIZATION, "profile", profile_id,
        root=str(root), home=home, execute=True, approve=True,
        runner=runner, target=MINT, is_root=True,
    )
    assert second.ok is True
    assert second.files_applied is True
    assert (home / ".config" / "kdeglobals").is_file()
    assert second.recovery_point           # el pipeline de archivos sí es reversible

    # El segundo pipeline no instala NADA: solo comprueba y escribe.
    nuevas = runner.calls[mark:]
    assert not any("install" in call for call in nuevas)


def test_the_environment_pipeline_is_idempotent(tmp_path: Path):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _profile(root)
    runner = _mint()

    pipelines.run(
        pipelines.ENVIRONMENT, "profile", profile_id, root=str(root), home=home,
        execute=True, approve=True, runner=runner, target=MINT, is_root=True,
    )
    mark = len(runner.calls)

    second = pipelines.run(
        pipelines.ENVIRONMENT, "profile", profile_id, root=str(root), home=home,
        execute=True, approve=True, runner=runner, target=MINT, is_root=True,
    )
    nuevas = runner.calls[mark:]

    # Nada se reinstala desde cero. Plasma tiene política «latest», así que sí se
    # le pide la versión más reciente — y como no había ninguna, queda igual.
    frescas = [
        call for call in nuevas if "install" in call and "--only-upgrade" not in call
    ]
    assert frescas == []
    assert second.ok is True
    assert all(
        item.status in (ItemStatus.ALREADY_PRESENT, ItemStatus.UPDATED)
        for item in second.plan.items
        if item.kind in ("desktop", "application")
    )


def test_the_authorization_is_renewed_before_every_privileged_command(tmp_path: Path):
    """Entre dos latidos puede caducar el ticket: se renueva antes de cada comando."""
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _profile(root)
    runner = _mint()

    pipelines.run(
        pipelines.ENVIRONMENT, "profile", profile_id, root=str(root), home=home,
        execute=True, approve=True, runner=runner, target=MINT, is_root=False,
    )

    renovaciones = 0
    privilegiados = 0
    for call in runner.calls:
        if call == ["sudo", "-n", "-v"]:
            renovaciones += 1
        elif call[:2] == ["sudo", "-n"]:
            privilegiados += 1
            # Cada comando privilegiado va precedido de una renovación.
            assert renovaciones >= privilegiados

    assert privilegiados >= 2      # el escritorio y la aplicación
    assert renovaciones >= privilegiados
