from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from styler.component_catalog.executors import BackupConfigExecutor, OverlayInstallExecutor, VerifyExecutor
from styler.component_catalog.gimp_runtime import (
    InitializeFlatpakAppExecutor, ResolveFlatpakAppFactsExecutor,
    _select_photogimp_source_version,
)
from styler.flatpak_facts import (
    FlatpakApplicationFacts,
    config_schema_from_version,
    save_flatpak_facts,
)
from styler.planning.models import ExecutionContext, StepDefinition


SOURCE = "https://github.com/Diolinux/PhotoGIMP/releases/download/3.0/PhotoGIMP-linux.zip"


def _response_with_photogimp_3() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as bundle:
        bundle.writestr(".config/GIMP/3.0/sessionrc", "photogimp-session")
        bundle.writestr(".config/GIMP/3.0/toolrc", "photogimp-tools")
        bundle.writestr(
            ".local/share/applications/photogimp.desktop",
            "[Desktop Entry]\nName=PhotoGIMP\n",
        )
    return payload.getvalue()


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_future_major_versions_are_normalized_without_hardcoding() -> None:
    assert config_schema_from_version("GIMP 4") == "4.0"
    assert config_schema_from_version("GIMP 4.2.7") == "4.2"
    assert config_schema_from_version("GIMP 10.3.1") == "10.3"


def test_source_template_selection_supports_future_target(tmp_path: Path) -> None:
    root = tmp_path / "GIMP"
    (root / "3.0").mkdir(parents=True)

    source, mode = _select_photogimp_source_version(root, "4.0")

    assert source == root / "3.0"
    assert mode == "forward-template"


def test_source_template_never_forces_newer_template_on_older_gimp(tmp_path: Path) -> None:
    root = tmp_path / "GIMP"
    (root / "4.0").mkdir(parents=True)

    with pytest.raises(ValueError, match="más nuevas"):
        _select_photogimp_source_version(root, "3.2")



def test_stored_3_2_facts_do_not_hide_an_upgrade_to_gimp_4(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    root = tmp_path / "library"
    config_root = home / ".var/app/org.gimp.GIMP/config/GIMP"
    (config_root / "3.2").mkdir(parents=True)
    save_flatpak_facts(
        root,
        FlatpakApplicationFacts(
            application_id="org.gimp.GIMP",
            installed=True,
            version="3.2.0",
            ref="app/org.gimp.GIMP/x86_64/stable",
            config_schema="3.2",
        ),
        config_root=str(config_root),
        config_path=str(config_root / "3.2"),
    )
    monkeypatch.setattr(
        "styler.component_catalog.gimp_runtime.inspect_flatpak_application",
        lambda app_id: FlatpakApplicationFacts(
            application_id=app_id,
            installed=True,
            version="4.0.1",
            ref="app/org.gimp.GIMP/x86_64/stable",
            config_schema="4.0",
        ),
    )
    step = StepDefinition(
        "app.gimp.resolve-facts",
        "resolve_flatpak_app_facts",
        config={
            "application_id": "org.gimp.GIMP",
            "config_root": "${HOME}/.var/app/org.gimp.GIMP/config/GIMP",
        },
    )

    result = ResolveFlatpakAppFactsExecutor().reconcile(
        step,
        ExecutionContext(root=root, values={"home": str(home)}),
    )

    assert result is None

def test_photogimp_3_template_is_overlaid_on_gimp_4_after_full_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    home = tmp_path / "home"
    config_root = home / ".var/app/org.gimp.GIMP/config/GIMP"
    target = config_root / "4.0"
    target.mkdir(parents=True)
    (target / "sessionrc").write_text("gimp-original", encoding="utf-8")
    (target / "user-preserved.conf").write_text("keep-me", encoding="utf-8")

    save_flatpak_facts(
        root,
        FlatpakApplicationFacts(
            application_id="org.gimp.GIMP",
            installed=True,
            version="4.0.1",
            branch="stable",
            ref="app/org.gimp.GIMP/x86_64/stable",
            config_schema="4.0",
        ),
        config_root=str(config_root),
        config_path=str(target),
        initialization_completed=True,
        initialized_application_version="4.0.1",
        initialized_config_schema="4.0",
        initialized_config_path=str(target),
    )

    monkeypatch.setattr(
        "styler.component_catalog.photogimp_overlay.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "flatpak" else None,
    )
    monkeypatch.setattr(
        "styler.component_catalog.photogimp_overlay.run_step_command",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        InitializeFlatpakAppExecutor,
        "_flatpak_state",
        classmethod(lambda cls, app_id: (False, False, "cerrado")),
    )
    archive_bytes = _response_with_photogimp_3()
    monkeypatch.setattr(
        "styler.component_catalog.photogimp_overlay.urlopen",
        lambda request, timeout=45: Response(archive_bytes),
    )

    ctx = ExecutionContext(
        root=root,
        dry_run=False,
        run_id="run-4",
        values={"home": str(home), "change_id": "photogimp"},
    )
    backup_step = StepDefinition(
        "app.photogimp.backup",
        "backup_config",
        config={
            "backup_source": "${HOME}/.var/app/org.gimp.GIMP/config/GIMP",
            "runtime_facts_application_id": "org.gimp.GIMP",
            "require_initialized_cycle": True,
        },
    )
    backup = BackupConfigExecutor().run(backup_step, ctx)
    assert backup.success
    full_backup = Path(backup.data["backup"])
    assert (full_backup / "sessionrc").read_text(encoding="utf-8") == "gimp-original"
    assert (full_backup / "user-preserved.conf").read_text(encoding="utf-8") == "keep-me"

    install_step = StepDefinition(
        "app.photogimp.install",
        "install_overlay",
        config={
            "source": SOURCE,
            "target": "${HOME}/.var/app/org.gimp.GIMP/config/GIMP",
            "runtime_facts_application_id": "org.gimp.GIMP",
            "require_initialized_cycle": True,
            "required_backup_step_id": "app.photogimp.backup",
            "verify_overlay_manifest": True,
        },
    )
    installed = OverlayInstallExecutor().run(install_step, ctx)

    assert installed.success
    assert installed.data["source_config_version"] == "3.0"
    assert installed.data["gimp_config_version"] == "4.0"
    assert installed.data["adaptation_mode"] == "forward-template"
    assert (target / "sessionrc").read_text(encoding="utf-8") == "photogimp-session"
    assert (target / "toolrc").read_text(encoding="utf-8") == "photogimp-tools"
    assert (target / "user-preserved.conf").read_text(encoding="utf-8") == "keep-me"

    manifest = json.loads((config_root / ".photogimp-manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_config_version"] == "3.0"
    assert manifest["target_config_version"] == "4.0"
    assert manifest["adaptation_mode"] == "forward-template"

    verified = VerifyExecutor().run(
        StepDefinition(
            "app.photogimp.verify",
            "verify",
            config={
                "target": "${HOME}/.var/app/org.gimp.GIMP/config/GIMP",
                "checks": ["marker:photogimp", "photogimp:overlay"],
            },
        ),
        ctx,
    )
    assert verified.success
