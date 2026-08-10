from __future__ import annotations

from pathlib import Path

import pytest

from styler.advanced_restore import (
    AdvancedRestoreError,
    AdvancedRestoreSettings,
    CommandOutput,
    RestoreCandidate,
    candidates_for_application,
    candidates_for_capability,
    configure_settings,
    install_candidate,
    load_settings,
)
from styler.provenance.models import (
    ApplicationRecord,
    Confidence,
    Origin,
    OriginKind,
)


def _apt_record(version: str = "3.0") -> ApplicationRecord:
    return ApplicationRecord(
        app_id="apt:gimp",
        name="gimp",
        manager="apt",
        version=version,
        architecture="amd64",
        origin=Origin(
            kind=OriginKind.APT,
            confidence=Confidence.CONFIRMED,
            remote_name="stable/main",
        ),
    )


def _apt_runner(argv, _timeout):
    if list(argv[:2]) == ["apt-cache", "madison"]:
        return CommandOutput(
            0,
            """
      gimp | 3.0 | http://repo stable/main amd64 Packages
      gimp | 2.10 | http://archive oldstable/main amd64 Packages
      gimp | 3.2 | http://repo testing/main amd64 Packages
            """,
            "",
        )
    return CommandOutput(127, "", "unexpected command")


def _which_apt(name: str):
    return f"/usr/bin/{name}" if name in {"apt-cache", "apt-get", "sudo"} else None


def test_advanced_restore_is_disabled_by_default(tmp_path):
    settings = load_settings(tmp_path)
    assert settings.enabled is False
    assert settings.allow_repository_lookup is False
    assert settings.allow_alternative_versions is False
    assert settings.allow_provider_change is False
    assert settings.allow_installation is False


def test_enabling_requires_explicit_acknowledgement(tmp_path):
    with pytest.raises(AdvancedRestoreError):
        configure_settings(tmp_path, enabled=True)

    settings = configure_settings(
        tmp_path,
        enabled=True,
        allow_repository_lookup=True,
        allow_alternative_versions=True,
        acknowledge_risk=True,
    )
    assert settings.enabled is True
    assert load_settings(tmp_path).allow_alternative_versions is True


def test_disabling_revokes_all_sensitive_permissions(tmp_path):
    configure_settings(
        tmp_path,
        enabled=True,
        allow_repository_lookup=True,
        allow_alternative_versions=True,
        allow_provider_change=True,
        allow_installation=True,
        acknowledge_risk=True,
    )
    settings = configure_settings(tmp_path, enabled=False)
    assert settings.enabled is False
    assert settings.allow_repository_lookup is False
    assert settings.allow_alternative_versions is False
    assert settings.allow_provider_change is False
    assert settings.allow_installation is False


def test_exact_version_is_kept_and_alternatives_are_hidden_by_policy(tmp_path):
    settings = AdvancedRestoreSettings(enabled=True, allow_repository_lookup=True)
    result = candidates_for_application(
        _apt_record("3.0"),
        settings,
        capability="application.gimp",
        root=tmp_path,
        runner=_apt_runner,
        which=_which_apt,
    )
    assert [candidate.version for candidate in result.candidates] == ["3.0"]
    assert result.candidates[0].relation == "exact"


def test_older_version_is_presented_only_when_explicitly_allowed(tmp_path):
    settings = AdvancedRestoreSettings(
        enabled=True,
        allow_repository_lookup=True,
        allow_alternative_versions=True,
    )
    result = candidates_for_application(
        _apt_record("3.0"),
        settings,
        capability="application.gimp",
        root=tmp_path,
        runner=_apt_runner,
        which=_which_apt,
    )
    relations = {candidate.version: candidate.relation for candidate in result.candidates}
    assert relations == {"3.0": "exact", "2.10": "older", "3.2": "newer"}


def test_missing_exact_version_can_offer_an_older_one(tmp_path):
    settings = AdvancedRestoreSettings(
        enabled=True,
        allow_repository_lookup=True,
        allow_alternative_versions=True,
    )
    result = candidates_for_application(
        _apt_record("4.0"),
        settings,
        capability="application.gimp",
        root=tmp_path,
        runner=_apt_runner,
        which=_which_apt,
    )
    assert result.exact_available is False
    assert any(candidate.version == "3.2" and candidate.relation == "older" for candidate in result.candidates)


def test_alternative_version_needs_second_approval_even_after_feature_enabled():
    candidate = RestoreCandidate(
        candidate_id="cand-demo",
        capability="application.gimp",
        manager="apt",
        name="gimp",
        version="2.10",
        source_type="repository",
        source="archive",
        relation="older",
        same_provider=True,
    )
    settings = AdvancedRestoreSettings(enabled=True, allow_installation=True)
    with pytest.raises(AdvancedRestoreError, match="versión alternativa"):
        install_candidate(
            candidate,
            settings,
            approve=True,
            execute=False,
            which=_which_apt,
        )

    result = install_candidate(
        candidate,
        settings,
        approve=True,
        approve_alternative_version=True,
        execute=False,
        which=_which_apt,
    )
    assert result.executed is False
    assert "gimp=2.10" in result.command


def test_provider_change_needs_its_own_approval():
    candidate = RestoreCandidate(
        candidate_id="cand-flatpak",
        capability="application.gimp",
        manager="flatpak",
        name="org.gimp.GIMP",
        version="3.0",
        source_type="repository",
        source="flathub",
        remote="flathub",
        branch="stable",
        relation="exact",
        same_provider=False,
    )
    settings = AdvancedRestoreSettings(enabled=True, allow_installation=True)
    with pytest.raises(AdvancedRestoreError, match="cambia de gestor"):
        install_candidate(candidate, settings, approve=True, execute=False, which=lambda name: "/bin/flatpak" if name == "flatpak" else None)


def test_capability_search_can_find_known_apt_provider(tmp_path):
    settings = AdvancedRestoreSettings(
        enabled=True,
        allow_repository_lookup=True,
        allow_alternative_versions=True,
    )
    result = candidates_for_capability(
        "application.gimp",
        settings,
        desired_version="3.0",
        preferred_manager="apt",
        root=tmp_path,
        runner=_apt_runner,
        which=_which_apt,
    )
    assert result.candidates
    assert all(candidate.manager == "apt" for candidate in result.candidates)


def test_local_appimage_install_is_a_dry_run_until_execute(tmp_path):
    source = tmp_path / "Demo.AppImage"
    source.write_bytes(b"demo")
    candidate = RestoreCandidate(
        candidate_id="cand-appimage",
        capability="application.demo",
        manager="appimage",
        name="Demo",
        version="1.0",
        source_type="local-artifact",
        source="vault",
        artifact_path=str(source),
        relation="exact",
    )
    settings = AdvancedRestoreSettings(enabled=True, allow_installation=True)
    home = tmp_path / "home"
    result = install_candidate(
        candidate,
        settings,
        approve=True,
        execute=False,
        destination_home=home,
    )
    assert result.executed is False
    assert not (home / "Applications" / "Demo.AppImage").exists()

    installed = install_candidate(
        candidate,
        settings,
        approve=True,
        execute=True,
        destination_home=home,
    )
    target = home / "Applications" / "Demo.AppImage"
    assert installed.success is True
    assert target.read_bytes() == b"demo"
    assert target.stat().st_mode & 0o111


def test_unversioned_capability_candidate_is_available_not_an_alternative():
    candidate = RestoreCandidate(
        candidate_id="cand-unversioned",
        capability="application.gimp",
        manager="apt",
        name="gimp",
        version="3.0",
        source_type="repository",
        source="repo",
        relation="available",
    )
    settings = AdvancedRestoreSettings(enabled=True, allow_installation=True)
    result = install_candidate(
        candidate,
        settings,
        approve=True,
        execute=False,
        which=_which_apt,
    )
    assert result.success is True
    assert result.executed is False


def _appimagelauncher_record(version: str = "3.0.0-beta-2-gha287~96cb937") -> ApplicationRecord:
    return ApplicationRecord(
        app_id="apt:appimagelauncher",
        name="appimagelauncher",
        manager="apt",
        version=version,
        architecture="amd64",
        origin=Origin(kind=OriginKind.APT, confidence=Confidence.UNKNOWN),
    )


def _fake_appimagelauncher_releases(_repository: str):
    return [
        {
            "tag_name": "v3.0.0-beta-2-gha287",
            "draft": False,
            "assets": [
                {
                    "name": "appimagelauncher_3.0.0-beta-2-gha287~96cb937_amd64.deb",
                    "browser_download_url": "https://github.com/TheAssassin/AppImageLauncher/releases/download/v3.0.0-beta-2-gha287/appimagelauncher_3.0.0-beta-2-gha287~96cb937_amd64.deb",
                },
                {
                    "name": "appimagelauncher_3.0.0-beta-2-gha287~96cb937_arm64.deb",
                    "browser_download_url": "https://github.com/TheAssassin/AppImageLauncher/releases/download/v3.0.0-beta-2-gha287/appimagelauncher_3.0.0-beta-2-gha287~96cb937_arm64.deb",
                },
            ],
        }
    ]


def test_appimagelauncher_falls_back_to_official_github_release(tmp_path):
    settings = AdvancedRestoreSettings(enabled=True, allow_repository_lookup=True)
    result = candidates_for_application(
        _appimagelauncher_record(),
        settings,
        root=tmp_path,
        runner=lambda _argv, _timeout: CommandOutput(0, "", ""),
        which=_which_apt,
        release_fetcher=_fake_appimagelauncher_releases,
    )
    github = [item for item in result.candidates if item.source_type == "github-release"]
    assert len(github) == 1
    assert github[0].relation == "exact"
    assert github[0].architecture == "amd64"
    assert github[0].source_verified is True
    assert github[0].artifact_url.startswith("https://github.com/TheAssassin/AppImageLauncher/releases/download/")


def test_github_release_candidate_downloads_then_installs(tmp_path):
    candidate = RestoreCandidate(
        candidate_id="cand-appimagelauncher",
        capability="",
        manager="apt",
        name="appimagelauncher",
        version="3.0.0-beta-2-gha287~96cb937",
        architecture="amd64",
        source_type="github-release",
        source="GitHub Releases oficial",
        artifact_url="https://github.com/TheAssassin/AppImageLauncher/releases/download/v3/appimagelauncher_3.0.0_amd64.deb",
        asset_name="appimagelauncher_3.0.0_amd64.deb",
        relation="exact",
        source_verified=True,
    )
    settings = AdvancedRestoreSettings(enabled=True, allow_installation=True)
    calls = []

    def downloader(url, destination):
        calls.append((url, destination.name))
        destination.write_bytes(b"fake-deb")

    def runner(argv, _timeout):
        calls.append(tuple(argv))
        assert argv[-1].endswith("appimagelauncher_3.0.0_amd64.deb")
        return CommandOutput(0, "installed", "")

    result = install_candidate(
        candidate,
        settings,
        execute=True,
        approve=True,
        runner=runner,
        which=_which_apt,
        downloader=downloader,
    )
    assert result.success is True
    assert calls[0][1] == "appimagelauncher_3.0.0_amd64.deb"
    assert any(isinstance(call, tuple) and "apt-get" in " ".join(call) for call in calls[1:])


def test_with_privilege_prefers_pkexec_for_tui_install(monkeypatch):
    from styler import advanced_restore

    monkeypatch.setattr(advanced_restore.os, "geteuid", lambda: 1000)
    available = {"pkexec": "/usr/bin/pkexec", "sudo": "/usr/bin/sudo"}
    argv = advanced_restore._with_privilege(
        ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "x.deb"],
        available.get,
    )
    assert argv[:2] == ["pkexec", "/usr/bin/env"]
    assert "apt-get" in argv


def test_with_privilege_uses_noninteractive_sudo_as_fallback(monkeypatch):
    from styler import advanced_restore

    monkeypatch.setattr(advanced_restore.os, "geteuid", lambda: 1000)
    available = {"sudo": "/usr/bin/sudo"}
    argv = advanced_restore._with_privilege(["apt-get", "install", "x.deb"], available.get)
    assert argv[:2] == ["sudo", "-n"]
