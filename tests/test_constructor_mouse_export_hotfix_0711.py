"""Regresiones del hotfix 0.7.1.1: clic en filas y exportación de baselines."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from styler.baselines import BaselineDefinition, BaselineKind, BaselineService
from styler.portable import PackageType, inspect_package, read_artifact

from tests.test_change_constructor_070 import _inventory


def _custom_baseline(tmp_path: Path) -> tuple[BaselineService, BaselineDefinition]:
    service = BaselineService(tmp_path / "library", home=tmp_path / "home")
    definition = service.register_inventory(
        _inventory("baseline-hotfix"),
        kind=BaselineKind.CUSTOM,
        baseline_id="ubuntu-test-custom",
        name="Estado inicial personalizado",
        activate_after=True,
    )
    return service, definition


def test_constructor_rows_disable_text_selection_that_crashed_mouse_down():
    app_source = Path("styler/tui/app.py").read_text(encoding="utf-8")
    start = app_source.index("class ChangeConstructorScreen")
    end = app_source.index("class HistoryScreen")
    assert "ALLOW_SELECT = False" in app_source[start:end]

    widget_source = Path("styler/tui/constructor_widgets.py").read_text(encoding="utf-8")
    assert "class ConstructorStatic(Static):" in widget_source
    assert widget_source.count("ALLOW_SELECT = False") >= 2
    assert "yield ConstructorStatic" in widget_source


def test_custom_baseline_still_exports_as_a_baseline_package(tmp_path):
    service, definition = _custom_baseline(tmp_path)
    target = service.export_package(definition.baseline_id, tmp_path / "custom.stylerpkg")
    inspection = inspect_package(target)
    assert inspection.manifest.package_type is PackageType.BASELINE
    assert inspection.manifest.metadata["baseline_id"] == definition.baseline_id


def test_catalog_candidate_is_official_without_mutating_local_baseline(tmp_path):
    service, definition = _custom_baseline(tmp_path)
    target = service.export_catalog_candidate(
        definition.baseline_id,
        tmp_path / "catalog",
        clean_install_confirmed=True,
    )
    assert target.name == "ubuntu-24.04-desktop-wayland-stable-x86_64.stylerpkg"
    inspection = inspect_package(target)
    assert inspection.manifest.package_type is PackageType.BASELINE
    entry = inspection.manifest.artifacts[0]
    exported = BaselineDefinition.from_dict(
        json.loads(read_artifact(target, entry).decode("utf-8"))
    )
    assert exported.kind is BaselineKind.OFFICIAL
    assert exported.image.clean_install is True
    assert exported.baseline_id == "ubuntu-24.04-desktop-wayland-stable-x86_64"
    assert service.get(definition.baseline_id).kind is BaselineKind.CUSTOM

    destination = BaselineService(tmp_path / "next-version", home=tmp_path / "home")
    imported = destination.import_package(target, trust=True)
    assert imported.kind is BaselineKind.OFFICIAL
    assert destination.recommended(_inventory("target").system).baseline_id == exported.baseline_id


def test_catalog_candidate_requires_explicit_clean_install_confirmation(tmp_path):
    service, definition = _custom_baseline(tmp_path)
    with pytest.raises(Exception, match="instalación limpia"):
        service.export_catalog_candidate(definition.baseline_id, tmp_path / "catalog")


def test_clicking_constructor_row_does_not_start_text_selection(tmp_path):
    pytest.importorskip("textual")
    from styler.tui.app import ChangeConstructorScreen, StylerApp

    root = tmp_path / "library"
    home = tmp_path / "home"
    home.mkdir(parents=True)
    BaselineService(root, home=home).register_inventory(
        _inventory("baseline-click"),
        kind=BaselineKind.CUSTOM,
        baseline_id="clickable-baseline",
        name="Base seleccionable",
        activate_after=True,
    )

    async def scenario() -> None:
        app = StylerApp(root=str(root), home=str(home))
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#nav-tools")
            await pilot.pause()
            assert isinstance(app.screen, ChangeConstructorScreen)
            await pilot.click("#constructor-back")
            await pilot.pause()
            await pilot.click(".row-name")
            await pilot.pause()
            assert app.screen.focused_baseline is not None

    asyncio.run(scenario())
