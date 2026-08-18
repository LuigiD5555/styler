"""Pruebas del plano de aplicaciones: seleccionar, planear, instalar, verificar.

Ninguna prueba toca el sistema real: todo pasa por `FakeRunner`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from styler import restore as orchestrator
from styler.applications import (
    AppSpec,
    CommandResult,
    InstallStatus,
    applications_from_inventory,
    execute_plan,
    merge_applications,
    plan_installation,
    privilege_prefix,
)
from tests.support.fake_runner import FakeRunner
from styler.layers import Layer, save_layer
from styler.models import FileEntry
from styler.objectstore import ObjectStore
from styler.profiles import compose_applications, create_profile, save_profile
from styler.provenance.models import (
    ApplicationRecord,
    Confidence,
    InstallReason,
    Integrity,
    Inventory,
    Origin,
    OriginKind,
    SystemIdentity,
)
from styler.snapshot import Snapshot, save_snapshot
from styler.models import State
from styler.target import Target


# --------------------------------------------------------------------------- #
# Auxiliares
# --------------------------------------------------------------------------- #

def _record(name: str, manager: str = "apt", reason=InstallReason.EXPLICIT) -> ApplicationRecord:
    return ApplicationRecord(
        app_id=f"{manager}:{name}",
        name=name,
        manager=manager,
        version="1.0",
        install_reason=reason,
        origin=Origin(
            kind=OriginKind.APT,
            remote_name="noble/main",
            confidence=Confidence.CONFIRMED,
        ),
        integrity=Integrity(),
    )


def _inventory(records: list[ApplicationRecord]) -> Inventory:
    return Inventory(
        inventory_id="inv1",
        captured_at=0.0,
        distro="Test",
        system=SystemIdentity(distro_id="ubuntu", distro_version="24.04"),
        scope="apps",
        managers_seen=["apt"],
        applications=records,
    )


UBUNTU = Target(distro_id="linuxmint", family="ubuntu", pretty_name="Linux Mint 22.3")


def _apt_runner(**kwargs) -> FakeRunner:
    return FakeRunner(programs={"apt-get", "apt-cache", "dpkg-query", "sudo"}, **kwargs)


def _plan(apps, runner, **kwargs):
    kwargs.setdefault("target", UBUNTU)
    kwargs.setdefault("is_root", True)
    return plan_installation(apps, runner=runner, **kwargs)


# --------------------------------------------------------------------------- #
# Selección: qué aplicaciones son «mías»
# --------------------------------------------------------------------------- #

def test_dependencies_are_never_treated_as_my_applications():
    inventory = _inventory([
        _record("gimp"),
        _record("libgimp2.0", reason=InstallReason.DEPENDENCY),
    ])
    apps = applications_from_inventory(inventory)
    assert [app.app_id for app in apps] == ["apt:gimp"]


def test_baseline_labels_what_i_added_but_never_excludes_what_i_use():
    """Una app que venía con la distro y que uso sigue siendo mía: hay que reinstalarla."""
    baseline = _inventory([_record("firefox"), _record("nano")])
    current = _inventory([_record("firefox"), _record("nano"), _record("krita")])

    apps = applications_from_inventory(current, baseline)
    assert [app.app_id for app in apps] == ["apt:firefox", "apt:krita", "apt:nano"]
    assert {app.app_id: app.reason for app in apps}["apt:krita"] == "added-since-baseline"
    assert {app.app_id: app.reason for app in apps}["apt:firefox"] == "explicit"

    # Quien quiera solo lo añadido puede pedirlo explícitamente.
    only_added = applications_from_inventory(current, baseline, only_added=True)
    assert [app.app_id for app in only_added] == ["apt:krita"]


def test_application_without_confirmed_origin_is_not_promised_as_installable():
    record = _record("cosa-rara")
    record.origin = Origin(kind=OriginKind.UNKNOWN, confidence=Confidence.UNKNOWN)
    apps = applications_from_inventory(_inventory([record]))
    assert apps[0].reproducible is False

    # Ningún gestor de este equipo la ofrece → no se promete: instalación manual.
    runner = FakeRunner(programs={"apt-get", "apt-cache", "dpkg-query"}, offered=set())
    plan = _plan(apps, runner)
    assert plan.steps[0].status == InstallStatus.MANUAL_REQUIRED
    assert not plan.pending()


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #

def test_plan_is_idempotent_for_applications_already_installed():
    runner = _apt_runner(installed={"apt:gimp"})
    plan = _plan(
        [AppSpec(manager="apt", name="gimp"), AppSpec(manager="apt", name="krita")], runner
    )
    statuses = {step.app.app_id: step.status for step in plan.steps}
    assert statuses["apt:gimp"] == InstallStatus.ALREADY_INSTALLED
    assert statuses["apt:krita"] == InstallStatus.WILL_INSTALL
    assert [step.app.app_id for step in plan.pending()] == ["apt:krita"]


def test_a_flatpak_application_falls_back_to_the_native_package_of_the_target():
    """Sin Flatpak en el equipo, GIMP sigue siendo GIMP: se instala con apt."""
    runner = FakeRunner(
        programs={"apt-get", "apt-cache", "dpkg-query"}, offered={"apt:gimp"}
    )
    plan = _plan([AppSpec(manager="flatpak", name="org.gimp.GIMP")], runner)
    assert plan.steps[0].status == InstallStatus.WILL_INSTALL
    assert plan.steps[0].argv[-1] == "gimp"


def test_plan_declares_a_missing_package_manager_instead_of_failing_silently():
    # Ningún gestor del equipo ofrece esta aplicación y no hay equivalencia.
    runner = FakeRunner(programs={"apt-get", "apt-cache", "dpkg-query"}, offered=set())
    plan = _plan([AppSpec(manager="flatpak", name="com.rara.Cosa")], runner)
    assert plan.steps[0].status == InstallStatus.MANAGER_MISSING
    assert not plan.pending()


def test_an_apt_application_is_resolved_with_the_native_manager_of_another_distro():
    """El gestor original es una preferencia, no una identidad universal."""
    arch = Target(distro_id="arch", family="arch", pretty_name="Arch Linux")
    runner = FakeRunner(programs={"pacman"})
    plan = plan_installation(
        [AppSpec(manager="apt", name="krita", display_name="Krita")],
        runner=runner, target=arch, is_root=True,
    )
    step = plan.steps[0]
    assert step.status == InstallStatus.WILL_INSTALL
    assert step.argv == ["pacman", "-S", "--needed", "--noconfirm", "krita"]


def test_an_application_unknown_to_the_catalog_is_discovered_by_asking_the_manager():
    """Sin equivalencia escrita a mano: se le pregunta al gestor del destino."""
    fedora = Target(distro_id="fedora", family="fedora", pretty_name="Fedora 41")
    runner = FakeRunner(programs={"dnf", "rpm"}, offered={"dnf:cosa-rara"})
    plan = plan_installation(
        [AppSpec(manager="apt", name="cosa-rara")], runner=runner, target=fedora, is_root=True
    )
    assert plan.steps[0].status == InstallStatus.WILL_INSTALL
    assert plan.steps[0].argv == ["dnf", "install", "-y", "cosa-rara"]
    assert "descubierta" in plan.steps[0].message


def test_plan_uses_noninteractive_sudo_and_flatpak_stays_unprivileged():
    """sudo SIEMPRE con -n: Styler nunca lee una contraseña desde la interfaz."""
    runner = FakeRunner(programs={"apt-get", "apt-cache", "dpkg-query", "flatpak", "sudo"})
    plan = plan_installation(
        [
            AppSpec(manager="apt", name="krita"),
            AppSpec(manager="flatpak", name="org.kde.krita", remote="flathub"),
        ],
        runner=runner, target=UBUNTU, is_root=False,
    )
    argv = {step.app.manager: step.argv for step in plan.pending()}
    assert argv["apt"][:2] == ["sudo", "-n"]
    assert "DPkg::Lock::Timeout=300" in argv["apt"]      # espera el bloqueo, no lo rompe
    assert "DEBIAN_FRONTEND=noninteractive" in argv["apt"]
    assert argv["flatpak"] == ["flatpak", "install", "-y", "flathub", "org.kde.krita"]


def test_missing_privileges_are_reported_not_hidden():
    runner = FakeRunner(programs={"apt-get", "apt-cache", "dpkg-query"})  # sin sudo ni pkexec
    assert privilege_prefix(runner, is_root=False) == []
    plan = plan_installation(
        [AppSpec(manager="apt", name="krita")], runner=runner, target=UBUNTU, is_root=False
    )
    assert any("administrador" in warning for warning in plan.warnings)


# --------------------------------------------------------------------------- #
# Ejecución
# --------------------------------------------------------------------------- #

def test_nothing_is_installed_without_execute_and_approve(tmp_path: Path):
    runner = _apt_runner()
    plan = _plan([AppSpec(manager="apt", name="krita")], runner)

    dry = execute_plan(plan, root=tmp_path, execute=False, approve=True, runner=runner)
    assert dry.dry_run is True
    assert not any("install" in call for call in runner.calls)

    unapproved = execute_plan(plan, root=tmp_path, execute=True, approve=False, runner=runner)
    assert unapproved.outcomes[0].status == InstallStatus.SKIPPED
    assert not any("install" in call for call in runner.calls)


def test_execute_installs_and_verifies(tmp_path: Path):
    runner = _apt_runner()
    plan = _plan([AppSpec(manager="apt", name="krita")], runner)
    report = execute_plan(plan, root=tmp_path, execute=True, approve=True, runner=runner)

    assert any("install" in call and "krita" in call for call in runner.calls)
    assert report.success is True
    assert [item.app_id for item in report.installed] == ["apt:krita"]
    assert Path(report.report_path).is_file()


def test_a_failed_installation_is_reported_with_a_human_reason(tmp_path: Path):
    runner = _apt_runner(failing={"paquete-inexistente"})
    plan = _plan([AppSpec(manager="apt", name="paquete-inexistente")], runner)
    report = execute_plan(plan, root=tmp_path, execute=True, approve=True, runner=runner)
    assert report.success is False
    failure = report.failures[0]
    assert "paquete-inexistente" in failure.message
    assert Path(failure.log_path).is_file()


def test_success_is_not_claimed_when_the_manager_lies(tmp_path: Path):
    """Código de salida 0 pero la app no aparece: eso es un fallo, no un éxito."""
    runner = _apt_runner(lying={"krita"})   # sale 0 pero no deja nada instalado
    plan = _plan([AppSpec(manager="apt", name="krita")], runner)
    report = execute_plan(plan, root=tmp_path, execute=True, approve=True, runner=runner)
    assert report.success is False
    assert "no aparece instalada" in report.failures[0].message


def test_installing_warns_that_undo_will_not_uninstall(tmp_path: Path):
    runner = _apt_runner()
    plan = _plan([AppSpec(manager="apt", name="krita")], runner)
    report = execute_plan(plan, root=tmp_path, execute=True, approve=True, runner=runner)
    assert any("no desinstala" in warning for warning in report.warnings)


def test_merge_applications_does_not_duplicate_between_layers():
    merged = merge_applications([
        [AppSpec(manager="apt", name="gimp")],
        [AppSpec(manager="apt", name="gimp"), AppSpec(manager="flatpak", name="org.kde.krita")],
    ])
    assert [app.app_id for app in merged] == ["apt:gimp", "flatpak:org.kde.krita"]


# --------------------------------------------------------------------------- #
# Orquestación completa: aplicaciones + archivos
# --------------------------------------------------------------------------- #

def _profile_with_app(root: Path, home: Path) -> str:
    source = root / "kdeglobals"
    source.write_text("[General]\nColorScheme=Dark\n")
    checksum, _ = ObjectStore(root=str(root)).store_file(source)
    layer = Layer(
        layer_id="tema-1",
        part_id="tema-colores",
        title="Tema y colores",
        files=[FileEntry(path="${HOME}/.config/kdeglobals", checksum=checksum, size=10)],
        applications=[AppSpec(manager="apt", name="konsole", display_name="Konsole")],
    )
    save_layer(layer, root=str(root))
    profile = create_profile("Mi escritorio", [layer.layer_id])
    save_profile(profile, root=str(root))
    return profile.profile_id


def test_orchestrator_installs_applications_then_writes_files(tmp_path: Path):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _profile_with_app(root, home)
    runner = _apt_runner()

    outcome = orchestrator.apply_profile(
        profile_id, root=str(root), execute=True, approve=True, home=home,
        runner=runner, target=UBUNTU, is_root=True,
    )

    assert outcome.ok is True
    assert outcome.installed_apps == ["apt:konsole"]
    assert outcome.files_applied is True
    assert (home / ".config" / "kdeglobals").is_file()
    # El orden es parte del contrato: primero instalar, luego configurar.
    assert any("install" in call for call in runner.calls)


def test_orchestrator_refuses_to_write_files_if_an_application_fails(tmp_path: Path):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _profile_with_app(root, home)
    runner = _apt_runner(failing={"konsole"})

    outcome = orchestrator.apply_profile(
        profile_id, root=str(root), execute=True, approve=True, home=home,
        runner=runner, target=UBUNTU, is_root=True,
    )

    assert outcome.ok is False
    assert outcome.failed_apps == ["apt:konsole"]
    assert outcome.files_applied is False
    assert not (home / ".config" / "kdeglobals").exists()
    assert "NO copió ningún archivo" in outcome.aborted_reason


def test_orchestrator_preview_touches_nothing(tmp_path: Path):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _profile_with_app(root, home)
    runner = _apt_runner()

    outcome = orchestrator.apply_profile(
        profile_id, root=str(root), execute=False, approve=True, home=home,
        runner=runner, target=UBUNTU, is_root=True,
    )

    assert outcome.dry_run is True
    assert [step.app.app_id for step in outcome.install_plan.pending()] == ["apt:konsole"]
    assert not any("install" in call for call in runner.calls)
    assert not (home / ".config" / "kdeglobals").exists()


def test_no_apps_mode_applies_files_but_warns(tmp_path: Path):
    root = tmp_path / "lib"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    profile_id = _profile_with_app(root, home)
    runner = _apt_runner()

    outcome = orchestrator.apply_profile(
        profile_id, root=str(root), execute=True, approve=True, home=home,
        runner=runner, target=UBUNTU, is_root=True, install_apps=False,
    )

    assert outcome.files_applied is True
    assert outcome.installed_apps == []
    assert any("no instalarlas" in warning for warning in outcome.warnings)


def test_snapshot_applications_travel_into_layers_and_back(tmp_path: Path):
    from styler.layers import extract_layers

    root = tmp_path / "lib"
    root.mkdir()
    snapshot = Snapshot(
        snapshot_id="snap1234",
        label="Mi equipo",
        state=State(
            state_id="snap1234",
            label="Mi equipo",
            applications=[AppSpec(manager="flatpak", name="org.kde.krita", remote="flathub")],
        ),
    )
    save_snapshot(snapshot, root=str(root))

    layers = extract_layers(snapshot)
    app_layers = [layer for layer in layers if layer.part_id == "aplicaciones"]
    assert len(app_layers) == 1
    assert app_layers[0].restorable() is True
    save_layer(app_layers[0], root=str(root))

    apps = compose_applications(app_layers)
    assert [app.app_id for app in apps] == ["flatpak:org.kde.krita"]
