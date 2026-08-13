"""Ejecutores del catálogo. Cubren el comportamiento REAL (0.13).

Antes estos pasos fallaban explícito por no tener rutas declaradas. Ahora el
esquema TOML declara rutas (`config_root` por proveedor, `[resources.paths]`)
y los assets `catalog://` existen, así que operan de verdad. Lo que sigue
fallando explícito —y debe seguir haciéndolo— es lo que NO se puede resolver:
un destino sin declarar, una ruta fuera del HOME, un asset inexistente.
"""
from __future__ import annotations

import sys
import io
import tempfile
import zipfile

import pytest
from pathlib import Path
from types import SimpleNamespace

from styler.component_catalog.executors import (
    ApplyConfigExecutor,
    BackupConfigExecutor,
    OverlayInstallExecutor,
    VerifyExecutor,
    _backup_existing_file,
    _copy_tree_contents,
    extended_registry,
)
from styler.runtime.models import ExecutionContext, StepDefinition


def _ctx(root: Path, dry_run: bool, home: Path | None = None) -> ExecutionContext:
    values = {"home": str(home)} if home else {}
    return ExecutionContext(root=root, dry_run=dry_run, values=values)


def _step(step_type: str, config: dict) -> StepDefinition:
    return StepDefinition(id="x.test", step_type=step_type, config=config)


# --------------------------------------------------------------------------- #
# VerifyExecutor
# --------------------------------------------------------------------------- #

def test_verify_sin_checks_falla():
    with tempfile.TemporaryDirectory() as tmp:
        result = VerifyExecutor().run(_step("verify", {"checks": []}), _ctx(Path(tmp), False))
        assert not result.success
        assert result.data["error_code"] == "NO_VERIFICATION_CHECKS"


def test_verify_ejecutable_presente():
    python = Path(sys.executable).name
    with tempfile.TemporaryDirectory() as tmp:
        result = VerifyExecutor().run(
            _step("verify", {"checks": [f"executable:{python}"]}), _ctx(Path(tmp), False)
        )
        assert result.success


def test_verify_ejecutable_ausente_falla():
    with tempfile.TemporaryDirectory() as tmp:
        result = VerifyExecutor().run(
            _step("verify", {"checks": ["executable:no-existe-jamas-xyz"]}), _ctx(Path(tmp), False)
        )
        assert not result.success
        assert result.data["error_code"] == "VERIFICATION_FAILED"


def test_verify_directorio_real_se_comprueba_de_verdad():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        target = home / ".config" / "GIMP"
        target.mkdir(parents=True)
        result = VerifyExecutor().run(
            _step("verify", {"checks": ["directory:user-config:gimp"], "target": str(target)}),
            _ctx(Path(tmp), False, home=home),
        )
        assert result.success


def test_verify_check_sin_ruta_declarada_falla_explicito():
    with tempfile.TemporaryDirectory() as tmp:
        result = VerifyExecutor().run(
            _step("verify", {"checks": ["directory:user-config:gimp"]}), _ctx(Path(tmp), False)
        )
        assert not result.success
        assert result.data["error_code"] == "NO_TARGET_FOR_CHECK"


def test_verify_dry_run_no_ejecuta_comprobaciones():
    with tempfile.TemporaryDirectory() as tmp:
        result = VerifyExecutor().run(
            _step("verify", {"checks": ["executable:no-existe-jamas-xyz"]}), _ctx(Path(tmp), True)
        )
        assert result.success
        assert result.status == "dry_run"


# --------------------------------------------------------------------------- #
# BackupConfigExecutor
# --------------------------------------------------------------------------- #

def test_backup_sin_fuente_declarada_falla_explicito():
    with tempfile.TemporaryDirectory() as tmp:
        result = BackupConfigExecutor().run(_step("backup_config", {}), _ctx(Path(tmp), False))
        assert not result.success
        assert result.data["error_code"] == "NO_BACKUP_SOURCE"


def test_backup_dry_run():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        result = BackupConfigExecutor().run(
            _step("backup_config", {"backup_source": "${HOME}/.config/GIMP"}),
            _ctx(Path(tmp), True, home=home),
        )
        assert result.success
        assert result.status == "dry_run"


def test_backup_fuente_inexistente_no_falla():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        result = BackupConfigExecutor().run(
            _step("backup_config", {"backup_source": "${HOME}/no-existe"}),
            _ctx(Path(tmp), False, home=home),
        )
        assert result.success
        assert result.data["existed"] is False


def test_backup_real_copia_el_directorio():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        source = home / ".config" / "GIMP"
        source.mkdir(parents=True)
        (source / "settings.conf").write_text("valor", encoding="utf-8")

        result = BackupConfigExecutor().run(
            _step("backup_config", {"backup_source": "${HOME}/.config/GIMP"}),
            _ctx(Path(tmp), False, home=home),
        )
        assert result.success
        backup_path = Path(result.data["backup"])
        assert (backup_path / "settings.conf").read_text(encoding="utf-8") == "valor"


def test_backup_rechaza_fuente_fuera_del_home():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        result = BackupConfigExecutor().run(
            _step("backup_config", {"backup_source": "/etc/shadow"}),
            _ctx(Path(tmp), False, home=home),
        )
        assert not result.success
        assert result.data["error_code"] == "UNSAFE_BACKUP_SOURCE"


# --------------------------------------------------------------------------- #
# Overlay / apply_config
# --------------------------------------------------------------------------- #

def test_overlay_dry_run_no_escribe():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        target = home / ".config" / "GIMP"
        target.mkdir(parents=True)
        result = OverlayInstallExecutor().run(
            _step("install_overlay", {"source": "catalog://photogimp", "target": str(target)}),
            _ctx(Path(tmp), True, home=home),
        )
        assert result.success
        assert result.status == "dry_run"
        assert not (target / ".photogimp-marker").exists()



def test_photogimp_remoto_descarga_y_extrae_en_home(monkeypatch):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as bundle:
        bundle.writestr(".config/GIMP/3.0/gimprc", "photogimp")
        bundle.writestr(".local/share/applications/photogimp.desktop", "[Desktop Entry]")
    archive_bytes = payload.getvalue()

    class Response(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(
        "styler.component_catalog.executors.urlopen",
        lambda request, timeout=45: Response(archive_bytes),
    )
    monkeypatch.setattr("styler.component_catalog.executors.shutil.which", lambda name: "/usr/bin/flatpak")
    monkeypatch.setattr(
        "styler.component_catalog.executors.run_step_command",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        marker_target = home / ".config/GIMP"
        (marker_target / "3.2").mkdir(parents=True)
        result = OverlayInstallExecutor().run(
            _step(
                "install_overlay",
                {
                    "source": "https://github.com/Diolinux/PhotoGIMP/releases/download/3.0/PhotoGIMP-linux.zip",
                    "target": str(marker_target),
                },
            ),
            _ctx(Path(tmp), False, home=home),
        )
        assert result.success
        assert (marker_target / "3.2/gimprc").read_text() == "photogimp"
        desktop = home / ".local/share/applications/photogimp.desktop"
        assert desktop.exists()
        assert "Exec=flatpak run org.gimp.GIMP %U" in desktop.read_text()
        assert (marker_target / ".photogimp-marker").exists()


def test_photogimp_remoto_rechaza_zip_con_escape(monkeypatch):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as bundle:
        bundle.writestr("../escape", "no")
    archive_bytes = payload.getvalue()

    class Response(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(
        "styler.component_catalog.executors.urlopen",
        lambda request, timeout=45: Response(archive_bytes),
    )
    monkeypatch.setattr("styler.component_catalog.executors.shutil.which", lambda name: "/usr/bin/flatpak")
    monkeypatch.setattr(
        "styler.component_catalog.executors.run_step_command",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        target = home / ".var/app/org.gimp.GIMP/config/GIMP"
        (target / "3.2").mkdir(parents=True)
        result = OverlayInstallExecutor().run(
            _step(
                "install_overlay",
                {
                    "source": "https://github.com/Diolinux/PhotoGIMP/releases/download/3.0/PhotoGIMP-linux.zip",
                    "target": str(target),
                },
            ),
            _ctx(Path(tmp), False, home=home),
        )
        assert not result.success
        assert result.data["error_code"] == "REMOTE_OVERLAY_INSTALL_FAILED"
        assert not (Path(tmp) / "escape").exists()

def test_overlay_asset_inexistente_falla_explicito():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        target = home / ".config"
        target.mkdir(parents=True)
        result = OverlayInstallExecutor().run(
            _step("install_overlay", {"source": "catalog://no-existe", "target": str(target)}),
            _ctx(Path(tmp), False, home=home),
        )
        assert not result.success
        assert result.data["error_code"] == "UNRESOLVED_SOURCE_OR_TARGET"


def test_apply_config_sin_destino_falla_explicito():
    with tempfile.TemporaryDirectory() as tmp:
        result = ApplyConfigExecutor().run(_step("apply_config", {}), _ctx(Path(tmp), False))
        assert not result.success
        assert result.data["error_code"] == "NO_CONFIG_TARGET"


def test_apply_config_prepara_el_destino_real():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        result = ApplyConfigExecutor().run(
            _step("apply_config", {"target": "${HOME}/.config"}), _ctx(Path(tmp), False, home=home)
        )
        assert result.success
        assert (home / ".config").is_dir()


def test_registro_extendido_incluye_todos_los_tipos():
    known = extended_registry().known_types()
    assert {
        "install_package", "enable_service", "verify",
        "backup_config", "install_overlay", "apply_config",
    } <= known


def _photogimp_response(
    monkeypatch,
    desktop_content="[Desktop Entry]\nName=PhotoGIMP\n",
    desktop_name="photogimp.desktop",
):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as bundle:
        bundle.writestr(".config/GIMP/3.0/gimprc", "photogimp")
        bundle.writestr(f".local/share/applications/{desktop_name}", desktop_content)
    archive_bytes = payload.getvalue()

    class Response(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(
        "styler.component_catalog.executors.urlopen",
        lambda request, timeout=45: Response(archive_bytes),
    )
    monkeypatch.setattr("styler.component_catalog.executors.shutil.which", lambda name: "/usr/bin/flatpak")
    monkeypatch.setattr(
        "styler.component_catalog.executors.run_step_command",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )




def test_photogimp_maps_source_3_0_to_detected_3_2_and_rewrites_wmclass(monkeypatch, tmp_path):
    _photogimp_response(
        monkeypatch,
        desktop_content=(
            "[Desktop Entry]\n"
            "Name=PhotoGIMP\n"
            "Exec=flatpak run --command=gimp org.gimp.GIMP %U\n"
            "StartupWMClass=gimp-3.0\n"
        ),
        desktop_name="org.gimp.GIMP.desktop",
    )
    home = tmp_path / "home"
    marker_target = home / ".var/app/org.gimp.GIMP/config/GIMP"
    version = marker_target / "3.2"
    version.mkdir(parents=True)

    exported = home / ".local/share/flatpak/exports/share/applications/org.gimp.GIMP.desktop"
    exported.parent.mkdir(parents=True)
    exported.write_text(
        "[Desktop Entry]\n"
        "Exec=flatpak run --branch=stable --command=gimp --file-forwarding "
        "org.gimp.GIMP @@u %U @@\n"
        "StartupWMClass=gimp-3.2\n"
    )

    result = OverlayInstallExecutor().run(
        _step(
            "install_overlay",
            {
                "source": "https://github.com/Diolinux/PhotoGIMP/releases/download/3.0/PhotoGIMP-linux.zip",
                "target": str(marker_target),
            },
        ),
        _ctx(tmp_path, False, home=home),
    )

    assert result.success
    assert result.data["source_config_version"] == "3.0"
    assert result.data["gimp_config_version"] == "3.2"
    assert result.data["startup_wm_class"] == "gimp-3.2"
    assert (version / "gimprc").read_text() == "photogimp"
    assert not (marker_target / "3.0").exists()

    desktop = home / ".local/share/applications/org.gimp.GIMP.desktop"
    content = desktop.read_text()
    assert "StartupWMClass=gimp-3.2" in content
    assert "StartupWMClass=gimp-3.0" not in content
    assert "--branch=stable --command=gimp --file-forwarding" in content


def test_photogimp_wmclass_falls_back_to_detected_config_version(monkeypatch, tmp_path):
    _photogimp_response(
        monkeypatch,
        desktop_content=(
            "[Desktop Entry]\n"
            "Name=PhotoGIMP\n"
            "StartupWMClass=gimp-3.0\n"
        ),
    )
    home = tmp_path / "home"
    marker_target = home / ".var/app/org.gimp.GIMP/config/GIMP"
    (marker_target / "3.4").mkdir(parents=True)

    result = OverlayInstallExecutor().run(
        _step(
            "install_overlay",
            {
                "source": "https://github.com/Diolinux/PhotoGIMP/releases/download/3.0/PhotoGIMP-linux.zip",
                "target": str(marker_target),
            },
        ),
        _ctx(tmp_path, False, home=home),
    )

    assert result.success
    desktop = home / ".local/share/applications/photogimp.desktop"
    assert "StartupWMClass=gimp-3.4" in desktop.read_text()


def test_rewrite_launchers_false_conserves_the_downloaded_launcher(monkeypatch, tmp_path):
    _photogimp_response(monkeypatch)
    home = tmp_path / "home"
    marker_target = home / ".var/app/org.gimp.GIMP/config/GIMP"
    (marker_target / "3.2").mkdir(parents=True)
    result = OverlayInstallExecutor().run(
        _step(
            "install_overlay",
            {
                "source": "https://github.com/Diolinux/PhotoGIMP/releases/download/3.0/PhotoGIMP-linux.zip",
                "target": str(marker_target),
                "rewrite_launchers": False,
            },
        ),
        _ctx(tmp_path, False, home=home),
    )
    assert result.success
    desktop = home / ".local/share/applications/photogimp.desktop"
    assert "Exec=flatpak run" not in desktop.read_text()
    assert result.data["rewrite_launchers"] is False


def test_overlay_receipt_records_files_not_preexisting_directories(monkeypatch, tmp_path):
    from styler.receipts import ReceiptJournal, ReceiptKind

    _photogimp_response(monkeypatch)
    home = tmp_path / "home"
    marker_target = home / ".var/app/org.gimp.GIMP/config/GIMP"
    version = marker_target / "3.2"
    version.mkdir(parents=True)
    existing_folder = version / "plug-ins"
    existing_folder.mkdir()
    user_file = existing_folder / "my-plugin.py"
    user_file.write_text("usuario")

    ctx = _ctx(tmp_path, False, home=home)
    ctx.values["change_id"] = "photogimp"
    result = OverlayInstallExecutor().run(
        _step(
            "install_overlay",
            {
                "source": "https://github.com/Diolinux/PhotoGIMP/releases/download/3.0/PhotoGIMP-linux.zip",
                "target": str(marker_target),
            },
        ),
        ctx,
    )
    assert result.success
    receipts = [r for r in ReceiptJournal(tmp_path, "photogimp").entries() if r.kind == ReceiptKind.PATHS_WRITTEN]
    assert receipts
    data = receipts[-1].data
    assert str(existing_folder) not in data.get("created_directories", [])
    assert user_file.read_text() == "usuario"


def test_overlay_rejects_existing_file_symlink(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "gimprc").write_text("nuevo")
    destination = tmp_path / "destination"
    destination.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("usuario")
    (destination / "gimprc").symlink_to(outside)

    with pytest.raises(ValueError, match="simbólic"):
        _copy_tree_contents(
            source, destination, backup_root=tmp_path / "backups"
        )

    assert outside.read_text() == "usuario"
    assert (destination / "gimprc").is_symlink()


def test_overlay_rejects_symlinked_subdirectory(tmp_path):
    source = tmp_path / "source"
    (source / "plug-ins").mkdir(parents=True)
    (source / "plug-ins" / "photogimp.py").write_text("nuevo")
    destination = tmp_path / "destination"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (destination / "plug-ins").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="simbólic"):
        _copy_tree_contents(
            source, destination, backup_root=tmp_path / "backups"
        )

    assert not (outside / "photogimp.py").exists()


def test_backup_rejects_symlink_target(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("usuario")
    target = tmp_path / "target.txt"
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="simbólic"):
        _backup_existing_file(target, tmp_path / "backups")

    assert outside.read_text() == "usuario"
