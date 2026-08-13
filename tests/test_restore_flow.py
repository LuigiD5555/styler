"""Pruebas del flujo unificado de restauración.

La regla que estas pruebas defienden, y que ninguna versión futura debe romper:

    Styler no copia ningún archivo de configuración hasta que el entorno y las
    aplicaciones necesarias están instalados y verificados.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from styler import orchestrator
from styler import restore as restore_mod
from styler import target as target_mod
from styler.applications import AppSpec, FakeRunner
from styler.layers import Layer, save_layer
from styler.models import DesktopEnvironmentRecord, FileEntry
from styler.objectstore import ObjectStore
from styler.profiles import create_profile, save_profile
from styler.restore import ItemStatus


# --------------------------------------------------------------------------- #
# Auxiliares
# --------------------------------------------------------------------------- #

UBUNTU = target_mod.Target(distro_id="linuxmint", version="22.3", family="ubuntu",
                           pretty_name="Linux Mint 22.3")
ARCH = target_mod.Target(distro_id="arch", family="arch", pretty_name="Arch Linux")
FEDORA = target_mod.Target(distro_id="fedora", family="fedora", pretty_name="Fedora 41")


def _kde_profile(root: Path, apps: list[AppSpec] | None = None, files: bool = True) -> str:
    store = ObjectStore(root=str(root))
    entries: list[FileEntry] = []
    if files:
        for name, path in (
            ("kdeglobals", "${HOME}/.config/kdeglobals"),
            ("appletsrc", "${HOME}/.config/plasma-org.kde.plasma.desktop-appletsrc"),
            ("icono", "${HOME}/.icons/tema/index.theme"),
            ("konsolerc", "${HOME}/.config/konsolerc"),
        ):
            source = root / name
            source.write_text(name)
            checksum, _ = store.store_file(source)
            entries.append(FileEntry(path=path, checksum=checksum, size=len(name)))

    layer = Layer(
        layer_id="kde-1",
        part_id="tema-colores",
        title="Escritorio",
        desktop="kde-plasma",
        desktop_environments=[
            DesktopEnvironmentRecord(environment_id="kde-plasma", name="KDE Plasma")
        ],
        files=entries,
        applications=list(apps or []),
    )
    save_layer(layer, root=str(root))
    profile = create_profile("Mi Plasma", [layer.layer_id])
    save_profile(profile, root=str(root))
    return profile.profile_id


def _runner(**kwargs) -> FakeRunner:
    return FakeRunner(programs={"apt-get", "dpkg-query", "sudo"}, **kwargs)


# --------------------------------------------------------------------------- #
# Punto 3: el destino resuelve el paquete, no el equipo original
# --------------------------------------------------------------------------- #

def test_the_same_intention_resolves_to_a_different_package_per_distro():
    assert target_mod.resolve_desktop("kde-plasma", UBUNTU) == ("apt", "kde-plasma-desktop")
    assert target_mod.resolve_desktop("kde-plasma", ARCH) == ("pacman", "plasma-meta")
    assert target_mod.resolve_desktop("kde-plasma", FEDORA) == ("dnf", "@kde-desktop")
    assert target_mod.resolve_desktop(
        "kde-plasma", target_mod.Target(distro_id="debian", family="debian")
    ) == ("apt", "kde-standard")


def test_target_family_is_detected_from_os_release(tmp_path: Path):
    path = tmp_path / "os-release"
    path.write_text('ID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_ID="22.3"\n')
    target = target_mod.detect_target(path)
    assert target.family == "ubuntu"
    assert target.native_manager == "apt"


# --------------------------------------------------------------------------- #
# Punto 2: Plasma es un requisito previo, no un accidente de la lista de apps
# --------------------------------------------------------------------------- #

def test_plasma_is_a_requirement_even_if_it_is_not_in_the_application_list(tmp_path: Path):
    profile_id = _kde_profile(tmp_path)
    plan = orchestrator.plan_restore(
        "profile", profile_id, root=str(tmp_path), runner=_runner(), target=UBUNTU
    )
    desktop = [item for item in plan.items if item.kind == "desktop"]
    assert len(desktop) == 1
    assert desktop[0].status == ItemStatus.WILL_INSTALL
    assert desktop[0].argv[-1] == "kde-plasma-desktop"


def test_plasma_already_present_is_not_reinstalled(tmp_path: Path):
    runner = _runner()
    runner.programs.add("plasmashell")  # el escritorio ya existe y arranca
    plan = orchestrator.plan_restore(
        "profile", _kde_profile(tmp_path), root=str(tmp_path), runner=runner, target=UBUNTU
    )
    desktop = next(item for item in plan.items if item.kind == "desktop")
    assert desktop.status == ItemStatus.ALREADY_PRESENT


def test_unknown_distro_stops_instead_of_guessing_a_package(tmp_path: Path):
    plan = orchestrator.plan_restore(
        "profile",
        _kde_profile(tmp_path),
        root=str(tmp_path),
        runner=_runner(),
        target=target_mod.Target(distro_id="rarolinux"),
    )
    desktop = next(item for item in plan.items if item.kind == "desktop")
    assert desktop.status == ItemStatus.MANUAL_REQUIRED
    assert plan.can_apply is False


# --------------------------------------------------------------------------- #
# Punto 4: infraestructura antes que aplicaciones
# --------------------------------------------------------------------------- #

def test_flatpak_and_its_remote_are_installed_before_the_flatpak_application(tmp_path: Path):
    apps = [AppSpec(manager="flatpak", name="org.kde.krita", remote="flathub")]
    plan = orchestrator.plan_restore(
        "profile",
        _kde_profile(tmp_path, apps),
        root=str(tmp_path),
        runner=_runner(),           # sin flatpak instalado
        target=UBUNTU,
    )
    stages = [item.stage for item in plan.items]
    assert stages.index("gestores") < stages.index("remotos") < stages.index("aplicaciones")

    manager = next(item for item in plan.items if item.kind == "manager")
    assert manager.argv[-1] == "flatpak"          # el paquete que provee el gestor
    remote = next(item for item in plan.items if item.kind == "remote")
    assert remote.status == ItemStatus.WILL_ADD
    assert "flathub" in remote.argv

    # La app de Flatpak NO se marca como bloqueada solo porque el gestor aún no
    # existe: se instalará antes, en su etapa.
    app = next(item for item in plan.items if item.kind == "application")
    assert app.status == ItemStatus.WILL_INSTALL
    assert plan.can_apply is True


def test_third_party_apt_repository_is_declared_and_blocks_instead_of_adding_keys(tmp_path: Path):
    apps = [
        AppSpec(
            manager="apt",
            name="brave-browser",
            remote="brave",
            remote_url="https://brave-browser-apt-release.s3.brave.com",
        )
    ]
    # El equipo no puede obtener Brave por ninguna vía configurada.
    runner = FakeRunner(programs={"apt-get", "apt-cache", "dpkg-query", "sudo"}, offered=set())
    plan = orchestrator.plan_restore(
        "profile",
        _kde_profile(tmp_path, apps),
        root=str(tmp_path),
        runner=runner,
        target=UBUNTU,
    )
    repo = next(item for item in plan.items if item.kind == "repository")
    assert repo.status == ItemStatus.MANUAL_REQUIRED
    assert plan.can_apply is False
    # Styler jamás propone un comando que instale una llave de terceros.
    assert repo.argv == []


# --------------------------------------------------------------------------- #
# Puntos 5 y 6: detenerse y verificar antes de tocar archivos
# --------------------------------------------------------------------------- #

def test_no_file_is_written_when_the_desktop_installation_fails(tmp_path: Path):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _kde_profile(root)
    runner = _runner(failing={"kde-plasma-desktop"})

    outcome = orchestrator.apply_profile(
        profile_id, root=str(root), execute=True, approve=True, home=home,
        runner=runner, privilege="none", target=UBUNTU, is_root=True,
    )

    assert outcome.ok is False
    assert outcome.files_applied is False
    assert list(home.rglob("*")) == []
    assert "NO copió ningún archivo" in outcome.aborted_reason


def test_an_installed_desktop_that_does_not_verify_is_not_a_success(tmp_path: Path):
    """apt dice «ok» pero plasmashell no aparece: eso NO es un escritorio."""
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _kde_profile(root)
    runner = _runner()  # instala bien, pero nunca aparece «plasmashell»

    outcome = orchestrator.apply_profile(
        profile_id, root=str(root), execute=True, approve=True, home=home,
        runner=runner, privilege="none", target=UBUNTU, is_root=True,
    )

    desktop = next(item for item in outcome.plan.items if item.kind == "desktop")
    assert desktop.status == ItemStatus.VERIFICATION_FAILED
    assert outcome.files_applied is False
    assert list(home.rglob("*")) == []


def test_pending_requirements_after_a_failure_are_not_reported_as_success(tmp_path: Path):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    apps = [AppSpec(manager="apt", name="krita")]
    profile_id = _kde_profile(root, apps)
    runner = _runner(failing={"kde-plasma-desktop"})

    outcome = orchestrator.apply_profile(
        profile_id, root=str(root), execute=True, approve=True, home=home,
        runner=runner, privilege="none", target=UBUNTU, is_root=True,
    )
    report = outcome.report
    assert report.failed  # el escritorio
    assert "Krita" in " ".join(report.pending) or "krita" in " ".join(report.pending)
    assert report.ok is False


# --------------------------------------------------------------------------- #
# Flujo feliz completo, con verificación y orden de archivos
# --------------------------------------------------------------------------- #

def _verifying_runner() -> FakeRunner:
    """Un equipo donde instalar sí deja el software disponible."""

    class Verifying(FakeRunner):
        def run(self, argv, timeout=None):
            result = super().run(argv, timeout)
            if result.returncode == 0 and "install" in argv:
                plasma_packages = {
                    "kde-plasma-desktop",  # Debian/Ubuntu family variant
                    "kde-standard",        # Debian
                    "plasma-meta",         # Arch
                    "@kde-desktop",        # Fedora group
                    "patterns-kde-kde_plasma",  # openSUSE
                }
                if plasma_packages.intersection(argv):
                    self.programs.add("plasmashell")
                if "flatpak" in argv:
                    self.programs.add("flatpak")
            return result

    return Verifying(programs={"apt-get", "dpkg-query", "sudo"})


def test_full_flow_installs_verifies_and_then_restores_files_in_order(tmp_path: Path):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    apps = [AppSpec(manager="apt", name="krita", display_name="Krita")]
    profile_id = _kde_profile(root, apps)
    runner = _verifying_runner()

    stages: list[str] = []
    outcome = orchestrator.apply_profile(
        profile_id, root=str(root), execute=True, approve=True, home=home,
        runner=runner, privilege="none", target=UBUNTU, is_root=True,
        progress=lambda stage, current, total, message: stages.append(stage),
    )

    assert outcome.ok is True
    assert outcome.report.verification.ok is True
    assert outcome.files_applied is True
    assert (home / ".config" / "kdeglobals").is_file()
    assert (home / ".icons" / "tema" / "index.theme").is_file()

    # Orden duro: instalar → verificar → archivos.
    assert stages.index("install") < stages.index("verify") < stages.index("files")

    # Orden de archivos: primero Plasma, al final los recursos personales.
    paths = [entry.path for entry in restore_mod.ordered_entries(outcome.plan)]
    assert paths.index("${HOME}/.config/kdeglobals") < paths.index(
        "${HOME}/.config/plasma-org.kde.plasma.desktop-appletsrc"
    )
    assert paths.index("${HOME}/.config/konsolerc") < paths.index(
        "${HOME}/.icons/tema/index.theme"
    )

    # Punto de recuperación creado, y aviso de reinicio de sesión.
    assert outcome.report.recovery_point
    assert outcome.report.needs_relogin is True


def test_the_plan_is_visible_and_approved_once(tmp_path: Path):
    plan = orchestrator.plan_restore(
        "profile",
        _kde_profile(tmp_path, [AppSpec(manager="apt", name="krita", display_name="Krita")]),
        root=str(tmp_path),
        runner=_runner(),
        target=UBUNTU,
    )
    summary = "\n".join(plan.human_summary())
    assert "Se instalará:" in summary
    assert "Krita" in summary
    assert "Después se restaurarán:" in summary
    assert "Paneles y widgets" in summary


def test_a_user_can_deliberately_skip_a_requirement(tmp_path: Path):
    apps = [AppSpec(manager="apt", name="krita", display_name="Krita", reproducible=False)]
    profile_id = _kde_profile(tmp_path, apps)

    blocked = orchestrator.plan_restore(
        "profile", profile_id, root=str(tmp_path), runner=_runner(), target=UBUNTU
    )
    assert blocked.can_apply is False  # una app no reinstalable bloquea

    skipped = orchestrator.plan_restore(
        "profile", profile_id, root=str(tmp_path), runner=_runner(), target=UBUNTU,
        skip=["apt:krita"],
    )
    item = next(item for item in skipped.items if item.kind == "application")
    assert item.status == ItemStatus.SKIPPED_BY_USER
    # Omitida a propósito no es lo mismo que «ya estaba instalada».
    assert item.status != ItemStatus.ALREADY_PRESENT


def test_nothing_runs_without_explicit_approval(tmp_path: Path):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    runner = _verifying_runner()
    outcome = orchestrator.apply_profile(
        _kde_profile(root), root=str(root), execute=True, approve=False, home=home,
        runner=runner, privilege="none", target=UBUNTU, is_root=True,
    )
    assert outcome.ok is False
    assert "aprobación" in outcome.aborted_reason
    assert not any("install" in call for call in runner.calls)
    assert list(home.rglob("*")) == []
