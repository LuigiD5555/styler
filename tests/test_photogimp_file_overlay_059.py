from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

from styler.component_catalog.executors import (
    CopyEffects,
    OverlayInstallExecutor,
    _copy_files_individually,
    _find_photogimp_payload,
    _select_photogimp_template_from_archive,
)
from styler.runtime.models import ExecutionContext, StepDefinition

SOURCE = "https://github.com/Diolinux/PhotoGIMP/releases/download/3.0/PhotoGIMP-linux.zip"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def _step(target: Path) -> StepDefinition:
    return StepDefinition(
        id="app.photogimp.install",
        step_type="install_overlay",
        config={
            "source": SOURCE,
            "target": str(target),
            "rewrite_launchers": True,
        },
    )


def test_hidden_payload_is_found_at_arbitrary_safe_depth(tmp_path: Path) -> None:
    payload = tmp_path / "wrapper" / "another-wrapper" / "PhotoGIMP"
    template = payload / ".config" / "GIMP" / "3.0"
    template.mkdir(parents=True)
    (template / "sessionrc").write_text("photogimp", encoding="utf-8")
    (payload / ".local" / "share").mkdir(parents=True)

    assert _find_photogimp_payload(tmp_path) == payload
    root, source, mode, selected_payload = _select_photogimp_template_from_archive(
        tmp_path,
        "4.0",
    )
    assert root == payload / ".config" / "GIMP"
    assert source == template
    assert mode == "forward-template"
    assert selected_payload == payload


def test_copy_is_file_by_file_and_merges_3_0_into_4_0(tmp_path: Path) -> None:
    source = tmp_path / "download" / ".config" / "GIMP" / "3.0"
    source.mkdir(parents=True)
    (source / "sessionrc").write_text("photogimp-session", encoding="utf-8")
    (source / "tool-options").mkdir()
    (source / "tool-options" / "gimp-crop-tool").write_text("crop", encoding="utf-8")

    destination = tmp_path / "home" / ".var" / "app" / "org.gimp.GIMP" / "config" / "GIMP" / "4.0"
    destination.mkdir(parents=True)
    (destination / "sessionrc").write_text("gimp-original", encoding="utf-8")
    (destination / "user-preserved.conf").write_text("keep", encoding="utf-8")

    effects = _copy_files_individually(
        source,
        destination,
        backup_root=tmp_path / "backups",
        effects=CopyEffects(),
    )

    assert (destination / "sessionrc").read_text(encoding="utf-8") == "photogimp-session"
    assert (destination / "tool-options" / "gimp-crop-tool").read_text(encoding="utf-8") == "crop"
    assert (destination / "user-preserved.conf").read_text(encoding="utf-8") == "keep"
    assert len(effects.verified_files) == 2
    session = next(item for item in effects.verified_files if item["relative_path"] == "sessionrc")
    assert session["operation"] == "replaced"
    assert Path(session["backup"]).read_text(encoding="utf-8") == "gimp-original"
    assert not (destination / "3.0").exists()


def test_installer_accepts_nested_hidden_layout_and_copies_resources_individually(
    monkeypatch,
    tmp_path: Path,
) -> None:
    archive = io.BytesIO()
    prefix = "outer/inner/PhotoGIMP"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(f"{prefix}/.config/GIMP/3.0/sessionrc", "photogimp-session")
        bundle.writestr(f"{prefix}/.config/GIMP/3.0/toolrc", "photogimp-tools")
        bundle.writestr(
            f"{prefix}/.local/share/applications/org.gimp.GIMP.desktop",
            "[Desktop Entry]\nName=PhotoGIMP\n",
        )
        bundle.writestr(
            f"{prefix}/.local/share/icons/hicolor/48x48/apps/photogimp.png",
            b"png-data",
        )
    archive_bytes = archive.getvalue()

    monkeypatch.setattr(
        "styler.component_catalog.executors.urlopen",
        lambda request, timeout=45: Response(archive_bytes),
    )
    monkeypatch.setattr(
        "styler.component_catalog.executors.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        "styler.component_catalog.executors.run_step_command",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    home = tmp_path / "home"
    marker_target = home / ".var/app/org.gimp.GIMP/config/GIMP"
    target = marker_target / "3.2"
    target.mkdir(parents=True)
    (target / "sessionrc").write_text("gimp-original", encoding="utf-8")

    result = OverlayInstallExecutor().run(
        _step(marker_target),
        ExecutionContext(root=tmp_path, dry_run=False, values={"home": str(home)}),
    )

    assert result.success, result.message
    assert (target / "sessionrc").read_text(encoding="utf-8") == "photogimp-session"
    assert (target / "toolrc").read_text(encoding="utf-8") == "photogimp-tools"
    assert (home / ".local/share/applications/org.gimp.GIMP.desktop").is_file()
    assert (home / ".local/share/icons/hicolor/48x48/apps/photogimp.png").is_file()
    assert result.data["files_copied_individually"] >= 4
    assert result.data["config_manifest_verified"] is True
    assert not (marker_target / "3.0").exists()


def test_large_zip_is_written_once_not_duplicated(monkeypatch, tmp_path: Path) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr(".config/GIMP/3.0/sessionrc", b"x" * (700 * 1024))
    archive_bytes = archive.getvalue()

    monkeypatch.setattr(
        "styler.component_catalog.executors.urlopen",
        lambda request, timeout=45: Response(archive_bytes),
    )
    monkeypatch.setattr(
        "styler.component_catalog.executors.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        "styler.component_catalog.executors.run_step_command",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    home = tmp_path / "home"
    marker_target = home / ".var/app/org.gimp.GIMP/config/GIMP"
    target = marker_target / "3.2"
    target.mkdir(parents=True)

    result = OverlayInstallExecutor().run(
        _step(marker_target),
        ExecutionContext(root=tmp_path, dry_run=False, values={"home": str(home)}),
    )

    assert result.success, result.message
    assert (target / "sessionrc").stat().st_size == 700 * 1024
