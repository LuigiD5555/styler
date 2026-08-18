from __future__ import annotations

import pytest

from styler import advanced_restore
from styler.advanced_restore import AdvancedRestoreSettings, CommandOutput, RestoreCandidate
from styler.component_graph import components_from_layers, resolve_component_graph
from styler.desktop_environment import KDE_INSTALL_URL, KDE_PROJECT_URL, detect_desktop_environments
from styler.layers import Layer
from styler.models import DesktopEnvironmentRecord, FileEntry, Package, State
from styler.observers.files_observer import PLASMA_ROOTS


def test_kde_is_registered_from_installed_package_even_without_active_session(monkeypatch):
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.delenv("DESKTOP_SESSION", raising=False)
    monkeypatch.delenv("XDG_SESSION_DESKTOP", raising=False)
    monkeypatch.setattr("styler.desktop_environment.shutil.which", lambda _name: None)

    records = detect_desktop_environments(
        [Package("apt", "kde-plasma-desktop", "6.3.5")]
    )

    assert len(records) == 1
    record = records[0]
    assert record.environment_id == "kde-plasma"
    assert record.version == "6.3.5"
    assert record.package_name == "kde-plasma-desktop"
    assert record.official_project_url == KDE_PROJECT_URL
    assert record.official_install_url == KDE_INSTALL_URL


def test_downloaded_theme_directories_are_not_copied_by_plasma_capture():
    roots = "\n".join(PLASMA_ROOTS)
    assert "/plasma/desktoptheme" not in roots
    assert "/plasma/look-and-feel" not in roots
    assert "/color-schemes" not in roots
    assert "/.themes" not in roots
    # Personal layout remains reproducible.
    assert "plasma-org.kde.plasma.desktop-appletsrc" in roots
    assert "kglobalshortcutsrc" in roots


def test_desktop_metadata_roundtrips_in_state_and_layer():
    environment = DesktopEnvironmentRecord(
        "kde-plasma",
        "KDE Plasma",
        "6.3.5",
        "apt",
        "kde-plasma-desktop",
        KDE_PROJECT_URL,
        KDE_INSTALL_URL,
        ["paquete apt:kde-plasma-desktop"],
    )
    state = State("state-1", "base", desktops=["kde-plasma"], desktop_environments=[environment])
    restored = State.from_dict(state.to_dict())
    assert restored.desktop_environments[0].official_install_url == KDE_INSTALL_URL

    layer = Layer(
        "paneles-1",
        "paneles",
        "Paneles",
        desktop="kde-plasma",
        desktop_environments=[environment],
        files=[FileEntry("${HOME}/.config/plasma-org.kde.plasma.desktop-appletsrc", "a" * 32)],
    )
    layer_restored = Layer.from_dict(layer.to_dict())
    assert layer_restored.desktop_environments[0].package_name == "kde-plasma-desktop"


def test_component_plan_places_kde_environment_before_panel_configuration():
    environment = DesktopEnvironmentRecord(
        "kde-plasma",
        "KDE Plasma",
        package_manager="apt",
        package_name="kde-plasma-desktop",
        official_project_url=KDE_PROJECT_URL,
        official_install_url=KDE_INSTALL_URL,
    )
    layer = Layer(
        "paneles-1",
        "paneles",
        "Paneles y widgets",
        desktop="kde-plasma",
        desktop_environments=[environment],
        files=[FileEntry("${HOME}/.config/plasma-org.kde.plasma.desktop-appletsrc", "b" * 32)],
    )

    plan = resolve_component_graph(components_from_layers([layer]))
    environment_id = "environment-kde-plasma"
    layer_id = "layer-paneles-1"
    assert plan.order.index(environment_id) < plan.order.index(layer_id)
    component = next(item for item in plan.components if item.component_id == layer_id)
    assert environment_id in component.depends_on


def test_kde_candidates_are_marked_as_official_and_unverified_ones_are_rejected():
    settings = AdvancedRestoreSettings(
        enabled=True,
        allow_repository_lookup=True,
        allow_installation=True,
    )

    def runner(argv, _timeout):
        name = argv[-1]
        return CommandOutput(0, f"{name} | 6.3.5 | https://archive.ubuntu.com/ubuntu noble/universe amd64 Packages\n", "")

    result = advanced_restore.candidates_for_capability(
        "desktop.kde-plasma",
        settings,
        preferred_manager="apt",
        runner=runner,
        which=lambda name: f"/usr/bin/{name}" if name == "apt-cache" else None,
    )
    assert result.candidates
    candidate = result.candidates[0]
    assert candidate.source_verified is True
    assert candidate.official_install_url == KDE_INSTALL_URL

    unverified = RestoreCandidate(
        candidate_id="unsafe-kde",
        capability="desktop.kde-plasma",
        manager="apt",
        name="plasma-desktop",
        source_type="repository",
        relation="available",
        source_verified=False,
    )
    with pytest.raises(advanced_restore.AdvancedRestoreError, match="ruta oficial de KDE"):
        advanced_restore.install_candidate(
            unverified,
            settings,
            approve=True,
            execute=False,
        )


def test_apt_official_source_check_rejects_spoofed_domains():
    assert advanced_restore._official_repository_source(
        "apt", "https://archive.ubuntu.com/ubuntu noble/universe amd64 Packages"
    )
    assert advanced_restore._official_repository_source(
        "apt", "https://deb.debian.org/debian bookworm/main amd64 Packages"
    )
    assert not advanced_restore._official_repository_source(
        "apt", "https://ubuntu.com.evil.example/repository noble/main"
    )
    assert not advanced_restore._official_repository_source(
        "apt", "https://evilubuntu.com/repository noble/main"
    )


def test_kde_install_refreshes_repository_and_does_not_pin_old_version():
    settings = AdvancedRestoreSettings(enabled=True, allow_repository_lookup=True, allow_installation=True)
    candidate = RestoreCandidate(
        candidate_id="kde-latest",
        capability="desktop.kde-plasma",
        manager="apt",
        name="kde-plasma-desktop",
        version="",
        source_type="repository",
        relation="available",
        source_verified=True,
        official_project_url=KDE_PROJECT_URL,
        official_install_url=KDE_INSTALL_URL,
    )
    calls = []

    def runner(argv, _timeout):
        calls.append(list(argv))
        return CommandOutput(0, "ok", "")

    result = advanced_restore.install_candidate(
        candidate, settings, approve=True, execute=True,
        runner=runner,
        which=lambda name: f"/usr/bin/{name}" if name in {"apt-get", "sudo"} else None,
    )
    assert result.success
    assert "DEBIAN_FRONTEND=noninteractive" in calls[0]
    assert "apt-get" in calls[0] and calls[0][-1] == "update"
    assert "DEBIAN_FRONTEND=noninteractive" in calls[1]
    assert calls[1][-2:] == ["install", "kde-plasma-desktop"]
