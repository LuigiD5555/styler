"""Pruebas de los fallos señalados en la revisión de la 0.10.0.

Cada prueba lleva el número del fallo que cierra. Si alguna se pone en rojo,
el fallo volvió.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from styler import base as base_mod
from styler import catalogs
from styler import orchestrator
from styler import resolution as resolution_mod
from styler import target as target_mod
from styler import verification as verify_mod
from styler.applications import AppSpec
from styler.layers import Layer, save_layer
from styler.models import DesktopEnvironmentRecord, FileEntry
from styler.objectstore import ObjectStore
from styler.profiles import create_profile, save_profile
from styler.restore import ItemStatus
from styler.runtime.commands import FakeRunner
from styler.target import Target

UBUNTU = Target(distro_id="linuxmint", family="ubuntu", pretty_name="Linux Mint 22.3")
ARCH = Target(distro_id="arch", family="arch", pretty_name="Arch Linux")
SUSE = Target(distro_id="opensuse-tumbleweed", family="suse", pretty_name="openSUSE")


def _profile(root: Path, apps=(), environment="kde-plasma", files=True) -> str:
    entries: list[FileEntry] = []
    if files:
        store = ObjectStore(root=str(root))
        source = root / "kdeglobals"
        source.write_text("[General]\n")
        checksum, _ = store.store_file(source)
        entries.append(
            FileEntry(path="${HOME}/.config/kdeglobals", checksum=checksum, size=10)
        )
    layer = Layer(
        layer_id="l1",
        part_id="tema-colores",
        title="Escritorio",
        desktop=environment,
        desktop_environments=(
            [DesktopEnvironmentRecord(environment_id=environment, name=environment)]
            if environment
            else []
        ),
        files=entries,
        applications=list(apps),
    )
    save_layer(layer, root=str(root))
    profile = create_profile("Perfil", [layer.layer_id])
    save_profile(profile, root=str(root))
    return profile.profile_id


def _mint_runner(**kwargs) -> FakeRunner:
    return FakeRunner(
        programs={"apt-get", "apt-cache", "dpkg-query", "sudo"},
        provides={"apt:kde-plasma-desktop": ("plasmashell",), "apt:flatpak": ("flatpak",)},
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Fallo 1 — Flatpak se preparaba, pero la aplicación quedaba sin comando
# --------------------------------------------------------------------------- #

def test_a_flatpak_application_gets_its_command_after_flatpak_is_installed(tmp_path: Path):
    """El comando NO se calcula al planear: se recalcula cuando el gestor existe."""
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    apps = [AppSpec(manager="flatpak", name="org.kde.krita", display_name="Krita",
                    remote="flathub")]
    profile_id = _profile(root, apps)
    runner = _mint_runner(offered={"apt:kde-plasma-desktop", "apt:flatpak", "flatpak:org.kde.krita"})

    plan = orchestrator.plan_restore(
        "profile", profile_id, root=str(root), runner=runner, target=UBUNTU, is_root=True
    )
    krita = next(item for item in plan.items if item.kind == "application")
    assert krita.status == ItemStatus.WILL_INSTALL
    assert krita.argv == []          # al planear, Flatpak todavía no existe

    outcome = orchestrator.apply_profile(
        profile_id, root=str(root), execute=True, approve=True, home=home,
        runner=runner, target=UBUNTU, is_root=True,
    )

    # Nunca se ejecutó un comando vacío…
    assert [] not in runner.calls
    # …y Krita terminó instalada con Flatpak, ya existente para entonces.
    assert ["flatpak", "install", "-y", "flathub", "org.kde.krita"] in runner.calls
    assert outcome.ok is True
    assert outcome.files_applied is True


# --------------------------------------------------------------------------- #
# Fallo 2 — Las aplicaciones no se adaptaban entre distribuciones
# --------------------------------------------------------------------------- #

def test_an_apt_profile_restores_on_arch_using_pacman(tmp_path: Path):
    apps = [AppSpec(manager="apt", name="krita", display_name="Krita")]
    profile_id = _profile(tmp_path, apps, environment="")
    runner = FakeRunner(programs={"pacman"}, offered={"pacman:krita"})

    plan = orchestrator.plan_restore(
        "profile", profile_id, root=str(tmp_path), runner=runner, target=ARCH, is_root=True
    )
    krita = next(item for item in plan.items if item.kind == "application")
    assert krita.status == ItemStatus.WILL_INSTALL
    assert krita.argv == ["pacman", "-S", "--needed", "--noconfirm", "krita"]


def test_the_catalog_translates_a_name_that_changes_between_distros():
    """En openSUSE, Firefox se llama MozillaFirefox. Eso vive en el catálogo, no en el código."""
    runner = FakeRunner(programs={"zypper", "rpm"}, offered={"zypper:MozillaFirefox"})
    requirement = resolution_mod.Requirement(
        kind="application", key="app:apt:firefox", title="Firefox",
        origin_manager="apt", origin_name="firefox",
    )
    result = resolution_mod.resolve_application(requirement, SUSE, runner)
    assert result.candidate.key == "zypper:MozillaFirefox"


# --------------------------------------------------------------------------- #
# Fallo 3 — No se aseguraba un Plasma reciente
# --------------------------------------------------------------------------- #

def test_an_existing_plasma_is_updated_not_just_accepted(tmp_path: Path):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _profile(root)
    runner = _mint_runner(
        installed={"apt:kde-plasma-desktop"},
        upgradable={"apt:kde-plasma-desktop"},
    )
    runner.programs.add("plasmashell")   # Plasma ya está, pero antiguo

    plan = orchestrator.plan_restore(
        "profile", profile_id, root=str(root), runner=runner, target=UBUNTU, is_root=True
    )
    desktop = next(item for item in plan.items if item.kind == "desktop")
    assert desktop.status == ItemStatus.WILL_UPDATE

    outcome = orchestrator.apply_profile(
        profile_id, root=str(root), execute=True, approve=True, home=home,
        runner=runner, target=UBUNTU, is_root=True,
    )
    desktop = next(item for item in outcome.plan.items if item.kind == "desktop")
    assert desktop.status == ItemStatus.UPDATED
    assert outcome.report.updated == [desktop.title]
    assert any("--only-upgrade" in call for call in runner.calls)


def test_a_plasma_already_at_the_latest_version_is_reported_as_already_present(tmp_path: Path):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _profile(root)
    runner = _mint_runner(installed={"apt:kde-plasma-desktop"})   # nada que actualizar
    runner.programs.add("plasmashell")

    outcome = orchestrator.apply_profile(
        profile_id, root=str(root), execute=True, approve=True, home=home,
        runner=runner, target=UBUNTU, is_root=True,
    )
    desktop = next(item for item in outcome.plan.items if item.kind == "desktop")
    assert desktop.status == ItemStatus.ALREADY_PRESENT
    assert outcome.ok is True


# --------------------------------------------------------------------------- #
# Fallo 4 — Un escritorio desconocido se daba por presente
# --------------------------------------------------------------------------- #

def test_an_unknown_desktop_is_not_assumed_present_and_blocks(tmp_path: Path):
    profile_id = _profile(tmp_path, environment="cosmic")
    runner = _mint_runner()

    plan = orchestrator.plan_restore(
        "profile", profile_id, root=str(tmp_path), runner=runner, target=UBUNTU, is_root=True
    )
    desktop = next(item for item in plan.items if item.kind == "desktop")
    assert desktop.status == ItemStatus.MANUAL_REQUIRED
    assert plan.can_apply is False
    assert "no significa presente" in desktop.detail


def test_a_new_desktop_can_be_taught_with_a_catalog_without_touching_python(tmp_path: Path):
    """Fallo 7: añadir COSMIC no debe obligar a modificar el código."""
    catalog_dir = tmp_path / ".styler" / "catalog"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "desktops.toml").write_text(
        """
[[capability]]
id = "cosmic"
title = "COSMIC"
verify_executables = ["cosmic-session"]

  [[capability.provider]]
  family = "ubuntu"
  manager = "apt"
  package = "cosmic-session"
""",
        encoding="utf-8",
    )
    catalogs.clear_cache()
    try:
        profile_id = _profile(tmp_path, environment="cosmic")
        runner = _mint_runner(offered={"apt:cosmic-session"})
        plan = orchestrator.plan_restore(
            "profile", profile_id, root=str(tmp_path), runner=runner, target=UBUNTU, is_root=True
        )
        desktop = next(item for item in plan.items if item.kind == "desktop")
        assert desktop.status == ItemStatus.WILL_INSTALL
        assert desktop.argv[-1] == "cosmic-session"
        assert plan.can_apply is True
    finally:
        catalogs.clear_cache()


# --------------------------------------------------------------------------- #
# Fallo 5 — La base personal restaurable no existía de verdad
# --------------------------------------------------------------------------- #

def test_the_restorable_base_is_an_explicit_collection_not_a_subtraction(tmp_path: Path):
    base = base_mod.RestorableBase()
    base.add_desktop("kde-plasma")
    base.add_application(AppSpec(manager="apt", name="firefox", display_name="Firefox"))
    base_mod.save(base, root=str(tmp_path))

    loaded = base_mod.load(root=str(tmp_path))
    assert loaded.desktop_environments == ["kde-plasma"]
    assert [app.app_id for app in loaded.applications] == ["apt:firefox"]


def test_the_restorable_base_is_reinstalled_even_if_the_profile_never_mentioned_it(tmp_path: Path):
    """Firefox venía con la distro y el perfil no lo trae. Sigue siendo mío."""
    root = tmp_path / "lib"
    root.mkdir()
    base = base_mod.RestorableBase()
    base.add_application(AppSpec(manager="apt", name="firefox", display_name="Firefox"))
    base_mod.save(base, root=str(root))

    profile_id = _profile(root, environment="")     # perfil sin aplicaciones
    runner = _mint_runner(offered={"apt:firefox"})
    plan = orchestrator.plan_restore(
        "profile", profile_id, root=str(root), runner=runner, target=UBUNTU, is_root=True
    )
    apps = [item.title for item in plan.items if item.kind == "application"]
    assert apps == ["Firefox"]


# --------------------------------------------------------------------------- #
# Fallo 6 — «No se pudo verificar» se convertía en éxito
# --------------------------------------------------------------------------- #

def test_an_indeterminate_verification_blocks_a_mandatory_application():
    runner = FakeRunner(programs=set())     # ningún gestor puede confirmar nada
    check = verify_mod.verify_candidate("Krita", None, runner, mandatory=True)
    assert check.ok is False
    assert check.mandatory is True
    assert check.indeterminate is True

    result = verify_mod.VerificationResult(checks=[check])
    assert result.ok is False               # indeterminado NO es éxito


def test_an_optional_application_may_stay_unverified():
    runner = FakeRunner(programs=set())
    check = verify_mod.verify_candidate("Extra", None, runner, mandatory=False)
    assert check.ok is False
    assert check.mandatory is False
    assert verify_mod.VerificationResult(checks=[check]).ok is True


# --------------------------------------------------------------------------- #
# Fallo 9 — Validación de dominios APT (regresión de seguridad)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "host",
    [
        "https://evilarchive.ubuntu.com/ubuntu",
        "https://archive.ubuntu.com.attacker.net/ubuntu",
        "https://notdeb.debian.org.evil.io/x",
    ],
)
def test_a_spoofed_domain_is_never_taken_as_official(host: str):
    assert target_mod.is_third_party_apt(host) is True


def test_real_official_domains_are_recognized():
    assert target_mod.is_third_party_apt("https://archive.ubuntu.com/ubuntu") is False
    assert target_mod.is_third_party_apt("https://mirror.archive.ubuntu.com/ubuntu") is False
    assert target_mod.is_third_party_apt("https://deb.debian.org/debian") is False


def test_a_commented_out_source_does_not_count_as_configured(tmp_path: Path):
    apt = tmp_path / "etc-apt"
    (apt / "sources.list.d").mkdir(parents=True)
    (apt / "sources.list").write_text(
        "# deb https://brave-browser-apt-release.s3.brave.com stable main\n"
        "deb https://archive.ubuntu.com/ubuntu noble main\n"
    )
    assert (
        target_mod.apt_repository_configured(
            "https://brave-browser-apt-release.s3.brave.com", apt
        )
        is False
    )
    assert target_mod.apt_repository_configured("https://archive.ubuntu.com/ubuntu", apt) is True


# --------------------------------------------------------------------------- #
# Fallo 10 — Refresco de catálogos para DNF y Zypper
# --------------------------------------------------------------------------- #

def test_every_supported_manager_knows_how_to_refresh_its_catalog():
    assert resolution_mod.refresh_argv("dnf", []) == ["dnf", "makecache", "--refresh"]
    assert resolution_mod.refresh_argv("zypper", []) == [
        "zypper", "--non-interactive", "refresh",
    ]
    assert resolution_mod.refresh_argv("pacman", []) == ["pacman", "-Sy", "--noconfirm"]
    assert "update" in resolution_mod.refresh_argv("apt", [])


def test_the_catalog_of_the_target_manager_is_refreshed_before_installing(tmp_path: Path):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _profile(root, [AppSpec(manager="apt", name="krita")], environment="")
    runner = FakeRunner(programs={"dnf", "rpm"}, offered={"dnf:krita"})

    orchestrator.apply_profile(
        profile_id, root=str(root), execute=True, approve=True, home=home,
        runner=runner, target=Target(distro_id="fedora", family="fedora"), is_root=True,
        refresh_index=True,
    )
    assert ["dnf", "makecache", "--refresh"] in runner.calls


# --------------------------------------------------------------------------- #
# Fallo 8 — Las pruebas no deben depender de la distribución donde corren
# --------------------------------------------------------------------------- #

def test_the_target_can_be_injected_so_tests_are_deterministic(tmp_path: Path):
    profile_id = _profile(tmp_path)
    runner = _mint_runner()
    for target, package in (
        (UBUNTU, "kde-plasma-desktop"),
        (ARCH, "plasma-meta"),
        (Target(distro_id="debian", family="debian"), "kde-standard"),
    ):
        runner = FakeRunner(
            programs={"apt-get", "apt-cache", "dpkg-query", "pacman", "sudo"}
        )
        plan = orchestrator.plan_restore(
            "profile", profile_id, root=str(tmp_path), runner=runner,
            target=target, is_root=True,
        )
        desktop = next(item for item in plan.items if item.kind == "desktop")
        assert desktop.argv[-1] == package
