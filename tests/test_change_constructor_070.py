"""Contrato del Constructor de cambios y del formato único de Styler."""
from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from styler.baselines import BaselineKind, BaselineService
from styler.change_recipe import compile_recipe, synthesize_recipe
from styler.component_catalog.executors import extended_registry
from styler.portable import PackageType, PortableLibrary, inspect_package
from styler.provenance.artifacts import checksum_path, scan_visual_artifacts
from styler.provenance.detectors import FakeRunner
from styler.provenance.detectors.appimage import AppImageDetector
from styler.provenance.models import (
    AppCategory,
    ApplicationRecord,
    ArtifactKind,
    Confidence,
    InstallReason,
    Integrity,
    Inventory,
    Origin,
    OriginKind,
    SystemArtifactRecord,
    SystemIdentity,
)
from styler.provenance import inventory as inventory_mod
from styler.ui.constructor import ChangeConstructorService


OLD_PUBLIC_SUFFIXES = (".stylerbase", ".stylerpack", ".stylermacro", ".styler.yaml", ".stl")


def _identity() -> SystemIdentity:
    return SystemIdentity(
        distro_id="ubuntu",
        distro_version="24.04",
        distro_variant="desktop",
        architecture="x86_64",
        desktop="GNOME",
        desktop_version="46",
        session_type="wayland",
        release_model="stable",
    )


def _inventory(identifier: str, *, applications=None, artifacts=None) -> Inventory:
    return Inventory(
        inventory_id=identifier,
        distro="ubuntu",
        system=_identity(),
        scope="all",
        managers_seen=["apt", "appimage"],
        applications=list(applications or []),
        artifacts=list(artifacts or []),
    )


def _apt_app(name: str = "stacer") -> ApplicationRecord:
    return ApplicationRecord(
        app_id=f"apt:{name}",
        name=name,
        display_name=name.capitalize(),
        manager="apt",
        version="1.0",
        architecture="amd64",
        install_method="repository",
        install_reason=InstallReason.EXPLICIT,
        category=AppCategory.DESKTOP_APP,
        origin=Origin(
            kind=OriginKind.APT,
            remote_name="noble/universe",
            remote_url="http://archive.ubuntu.com/ubuntu",
            confidence=Confidence.CONFIRMED,
        ),
    )


def _artifact(path: Path, home: Path, kind: ArtifactKind, name: str | None = None) -> SystemArtifactRecord:
    checksum, size, count, directory = checksum_path(path)
    portable = "${HOME}/" + path.relative_to(home).as_posix()
    return SystemArtifactRecord(
        artifact_id=f"artifact:{kind.value}:{portable}",
        kind=kind,
        name=name or path.name,
        path=portable,
        checksum=checksum,
        size=size,
        mode=stat.S_IMODE(path.stat().st_mode),
        is_directory=directory,
        file_count=count,
        scope="user",
    )


def test_only_stylerpkg_is_a_public_extension():
    roots = [
        Path("styler"), Path("docs"), Path("packaging"), Path("scripts"),
        Path("README.md"), Path("pyproject.toml"), Path("MANIFEST.in"),
    ]
    offenders: list[str] = []
    for root in roots:
        paths = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for suffix in OLD_PUBLIC_SUFFIXES:
                if suffix in text:
                    offenders.append(f"{path}: {suffix}")
    assert not offenders, "Todavía hay formatos públicos paralelos: " + "; ".join(offenders)


def test_baseline_round_trips_as_stylerpkg_and_custom_can_be_deleted(tmp_path):
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    service = BaselineService(source_root, home=tmp_path / "home")
    definition = service.register_inventory(
        _inventory("base0001"),
        kind=BaselineKind.CUSTOM,
        baseline_id="ubuntu-test",
        name="Ubuntu de prueba",
        activate_after=True,
    )
    package = service.export_package(definition.baseline_id, tmp_path / "ubuntu-test")
    assert package.suffix == ".stylerpkg"
    assert inspect_package(package).manifest.package_type is PackageType.BASELINE

    imported = BaselineService(destination_root, home=tmp_path / "home").import_package(
        package, activate_after=True
    )
    assert imported.baseline_id == definition.baseline_id
    destination = BaselineService(destination_root, home=tmp_path / "home")
    destination.remove(imported.baseline_id)
    assert all(item.baseline_id != imported.baseline_id for item in destination.list())
    assert destination.active(auto_select=False) is None


def test_visual_scanner_detects_theme_cursor_css_and_structured_setting(tmp_path):
    home = tmp_path / "home"
    (home / ".themes/Orchis").mkdir(parents=True)
    (home / ".themes/Orchis/gtk.css").write_text("window { color: red; }", encoding="utf-8")
    (home / ".icons/Bibata/cursors").mkdir(parents=True)
    (home / ".icons/Bibata/index.theme").write_text("[Icon Theme]", encoding="utf-8")
    (home / ".local/share/icons/Papirus").mkdir(parents=True)
    (home / ".local/share/icons/Papirus/index.theme").write_text("[Icon Theme]", encoding="utf-8")
    (home / ".local/share/backgrounds").mkdir(parents=True)
    (home / ".local/share/backgrounds/bosque.jpg").write_bytes(b"fake-jpeg")
    (home / ".local/share/fonts").mkdir(parents=True)
    (home / ".local/share/fonts/demo.ttf").write_bytes(b"fake-font")
    (home / ".config/gtk-3.0").mkdir(parents=True)
    (home / ".config/gtk-3.0/gtk.css").write_text("button { border-radius: 8px; }", encoding="utf-8")
    (home / ".config/rofi").mkdir(parents=True)
    (home / ".config/rofi/theme.rasi").write_text("window { transparency: real; }", encoding="utf-8")

    runner = FakeRunner(
        programs={"gsettings"},
        outputs={
            ("gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"): "'Orchis'\n",
            ("gsettings", "get", "org.gnome.desktop.interface", "cursor-theme"): "'Bibata'\n",
        },
    )
    records, problems = scan_visual_artifacts(home=home, runner=runner)
    assert problems == []
    kinds = {item.kind for item in records}
    assert {
        ArtifactKind.THEME,
        ArtifactKind.ICON_THEME,
        ArtifactKind.CURSOR_THEME,
        ArtifactKind.WALLPAPER,
        ArtifactKind.FONT,
        ArtifactKind.CSS,
        ArtifactKind.SETTING,
    } <= kinds
    assert any(item.setting_key == "gtk-theme" and item.setting_value == "'Orchis'" for item in records)


def test_appimage_detector_finds_local_executable(tmp_path):
    applications = tmp_path / "Applications"
    applications.mkdir()
    image = applications / "Demo-1.2.3-x86_64.AppImage"
    image.write_bytes(b"not-an-elf-but-a-local-appimage")
    image.chmod(0o755)
    records = AppImageDetector(search_dirs=[applications]).detect()
    assert [item.manager for item in records] == ["appimage"]
    assert records[0].integrity.artifact_path == str(image)
    assert records[0].integrity.artifact_available is True


def test_mixed_selection_generates_recipe_dag_and_one_package(tmp_path):
    home = tmp_path / "home"
    theme = home / ".themes/Orchis"
    theme.mkdir(parents=True)
    (theme / "gtk.css").write_text("window { color: red; }", encoding="utf-8")
    css = home / ".config/gtk-3.0/gtk.css"
    css.parent.mkdir(parents=True)
    css.write_text("button { padding: 4px; }", encoding="utf-8")
    appimage = home / "Applications/Demo.AppImage"
    appimage.parent.mkdir(parents=True)
    appimage.write_bytes(b"demo-appimage")
    appimage.chmod(0o755)

    image_record = ApplicationRecord(
        app_id="appimage:Demo",
        name="Demo",
        display_name="Demo",
        manager="appimage",
        version="1.0",
        install_reason=InstallReason.PORTABLE,
        category=AppCategory.DESKTOP_APP,
        origin=Origin(kind=OriginKind.APPIMAGE),
        integrity=Integrity(
            checksum="sha256:" + hashlib.sha256(appimage.read_bytes()).hexdigest(),
            artifact_path=str(appimage),
            artifact_available=True,
        ),
    )
    setting = SystemArtifactRecord(
        artifact_id="setting:gsettings:org.gnome.desktop.interface:gtk-theme",
        kind=ArtifactKind.SETTING,
        name="Tema GTK",
        path="setting://gsettings/org.gnome.desktop.interface/gtk-theme",
        checksum="sha256:test",
        scope="user",
        setting_backend="gsettings",
        setting_schema="org.gnome.desktop.interface",
        setting_key="gtk-theme",
        setting_value="'Orchis'",
    )
    artifacts = [_artifact(theme, home, ArtifactKind.THEME), _artifact(css, home, ArtifactKind.CSS), setting]
    synthesis = synthesize_recipe(
        "mi-cambio",
        "Mi cambio",
        [_apt_app(), image_record],
        artifacts,
        baseline_id="ubuntu-test",
        home=home,
    )
    assert {item.kind for item in synthesis.recipe.operations} == {
        "package.install", "asset.overlay", "setting.apply"
    }
    workflow = compile_recipe(synthesis.recipe)
    types = {step.step_type for step in workflow.steps}
    assert {"install_package", "install_overlay", "apply_visual_setting", "verify_generated_change"} <= types
    assert {"apply_visual_setting", "verify_generated_change"} <= extended_registry().known_types()

    root = tmp_path / "library"
    constructor = ChangeConstructorService(root=root, home=home)
    constructor.baselines.register_inventory(
        _inventory("base0002"), kind=BaselineKind.CUSTOM,
        baseline_id="ubuntu-test", name="Ubuntu test", activate_after=True,
    )
    current = _inventory(
        "current1", applications=[_apt_app(), image_record], artifacts=artifacts
    )
    inventory_mod.save_inventory(current, root=root)
    summary = constructor.summary()
    ids = {item.change_id for item in summary.detected}
    assert {"apt:stacer", "appimage:Demo", setting.artifact_id} <= ids
    constructor.select(ids)
    package = constructor.build_package(tmp_path / "mi-cambio", "mi-cambio", "Mi cambio")
    inspection = inspect_package(package.path)
    assert Path(package.path).suffix == ".stylerpkg"
    assert inspection.manifest.package_type is PackageType.CHANGE
    assert [item.kind for item in inspection.manifest.artifacts].count("recipe") == 1
    assert [item.kind for item in inspection.manifest.artifacts].count("graph") == 1
    installed = PortableLibrary(tmp_path / "imported").import_package(package.path)
    assert installed.manifest.package_id == "mi-cambio"


def test_constructor_inventory_mode_shows_explicit_apps_without_claiming_they_are_new(tmp_path):
    root = tmp_path / "library"
    home = tmp_path / "home"
    inventory_mod.save_inventory(_inventory("inventory-only", applications=[_apt_app()]), root=root)
    constructor = ChangeConstructorService(root=root, home=home)
    summary = constructor.summary()
    assert summary.inventory_only is True
    assert [item.change_id for item in summary.detected] == ["apt:stacer"]
    assert summary.detected[0].role == "inventario"


def test_local_apt_without_repository_is_detected_but_not_falsely_exportable(tmp_path):
    record = _apt_app("local-demo")
    record.install_method = "manual"
    record.origin = Origin(kind=OriginKind.APT, confidence=Confidence.UNKNOWN)
    record.integrity = Integrity(artifact_path=str(tmp_path / "local-demo.deb"), artifact_available=False)
    root = tmp_path / "library"
    constructor = ChangeConstructorService(root=root, home=tmp_path / "home")
    constructor.baselines.register_inventory(
        _inventory("base-local"), kind=BaselineKind.CUSTOM,
        baseline_id="base-local", name="Base local", activate_after=True,
    )
    inventory_mod.save_inventory(_inventory("current-local", applications=[record]), root=root)
    item = constructor.summary().detected[0]
    assert item.change_id == "apt:local-demo"
    assert item.exportable is False
    assert "repositorio" in item.reason.lower()


def test_tools_route_is_only_the_long_change_constructor():
    source = Path("styler/tui/app.py").read_text(encoding="utf-8")
    assert '"tools": ChangeConstructorScreen' in source
    assert "class ChangeConstructorScreen" in source
    assert "Constructor de cambios" in source
    assert "Paquetes guardados" in source
    assert "Eliminar personalizada" in source
    for obsolete in (
        "CaptureScreen", "ProfileLibraryScreen", "PackageGraphToolsScreen",
        "MonitorChangesScreen", "BaselineManagerScreen", "OriginsScreen",
    ):
        assert f"class {obsolete}" not in source


def test_constructor_tui_opens_and_switches_internal_tab(tmp_path):
    """Ejecuta el recorrido real cuando Textual está disponible."""

    import asyncio

    pytest.importorskip("textual")
    from styler.tui.app import ChangeConstructorScreen, StylerApp

    async def scenario() -> None:
        app = StylerApp(root=str(tmp_path / "library"), home=str(tmp_path / "home"))
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.click("#nav-tools")
            await pilot.pause()
            assert isinstance(app.screen, ChangeConstructorScreen)
            assert not app.screen.query_one("#constructor-new-view").has_class("hidden")
            await pilot.click("#constructor-tab-saved")
            await pilot.pause()
            assert app.screen.query_one("#constructor-new-view").has_class("hidden")
            assert not app.screen.query_one("#constructor-saved-view").has_class("hidden")

    asyncio.run(scenario())
