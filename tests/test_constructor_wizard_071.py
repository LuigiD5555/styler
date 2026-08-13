"""Contrato del constructor guiado 0.7.1."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from styler.baselines import BaselineKind
from styler.provenance import inventory as inventory_mod
from styler.provenance.models import Confidence, Integrity, Origin, OriginKind
from styler.ui.constructor import ChangeConstructorService, ConstructorError, describe_plan

from tests.test_change_constructor_070 import _apt_app, _inventory


def _ready_service(tmp_path: Path) -> ChangeConstructorService:
    root = tmp_path / "library"
    home = tmp_path / "home"
    service = ChangeConstructorService(root=root, home=home)
    service.baselines.register_inventory(
        _inventory("base-ready"),
        kind=BaselineKind.CUSTOM,
        baseline_id="base-ready",
        name="Base preparada",
        activate_after=True,
    )
    inventory_mod.save_inventory(
        _inventory("current-ready", applications=[_apt_app("stacer")]),
        root=root,
    )
    return service


def _service_with_non_exportable(tmp_path: Path) -> tuple[ChangeConstructorService, str]:
    root = tmp_path / "library"
    home = tmp_path / "home"
    bad = _apt_app("local-demo")
    bad.install_method = "manual"
    bad.origin = Origin(kind=OriginKind.APT, confidence=Confidence.UNKNOWN)
    bad.integrity = Integrity(
        artifact_path=str(tmp_path / "local-demo.deb"), artifact_available=False
    )
    service = ChangeConstructorService(root=root, home=home)
    service.baselines.register_inventory(
        _inventory("base-review"),
        kind=BaselineKind.CUSTOM,
        baseline_id="base-review",
        name="Base revisión",
        activate_after=True,
    )
    inventory_mod.save_inventory(
        _inventory("current-review", applications=[_apt_app("stacer"), bad]),
        root=root,
    )
    return service, bad.app_id


def test_select_rejects_non_exportable_with_its_reason(tmp_path):
    service, non_exportable = _service_with_non_exportable(tmp_path)
    with pytest.raises(ConstructorError) as error:
        service.select([non_exportable])
    assert "no puede empaquetarse" in str(error.value)
    assert service.summary().selected == ()


def test_select_all_exportable_ignores_the_ones_under_review(tmp_path):
    service, non_exportable = _service_with_non_exportable(tmp_path)
    summary = service.select_all_exportable()
    assert non_exportable not in summary.selected
    assert summary.selected


def test_summary_reads_the_inventory_from_disk_only_once(tmp_path, monkeypatch):
    service = _ready_service(tmp_path)
    calls = {"n": 0}
    original = inventory_mod.latest_inventory

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(inventory_mod, "latest_inventory", counted)
    service.invalidate()
    service.summary(); service.summary(); service.summary()
    assert calls["n"] == 1


def test_invalidate_forces_a_reread(tmp_path, monkeypatch):
    service = _ready_service(tmp_path)
    calls = {"n": 0}
    original = inventory_mod.latest_inventory

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(inventory_mod, "latest_inventory", counted)
    service.invalidate(); service.summary()
    service.invalidate(); service.summary()
    assert calls["n"] == 2


def test_build_package_synthesizes_the_recipe_once(tmp_path, monkeypatch):
    import styler.ui.constructor as module

    service = _ready_service(tmp_path)
    service.select_all_exportable()
    calls = {"n": 0}
    original = module.synthesize_recipe

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "synthesize_recipe", counted)
    plan = service.generated_plan("demo", "Demo")
    service.build_package(tmp_path / "out", "demo", "Demo", plan=plan)
    assert calls["n"] == 1


def test_build_package_regenerates_when_the_plan_is_for_another_baseline(tmp_path):
    service = _ready_service(tmp_path)
    service.select_all_exportable()
    plan = service.generated_plan("demo", "Demo")
    stale = type(plan)(**{**plan.__dict__, "baseline_id": "otra-linea-base"})
    result = service.build_package(tmp_path / "out2", "demo", "Demo", plan=stale)
    assert Path(result.path).is_file()


def test_plan_report_states_what_was_left_out(tmp_path):
    service, non_exportable = _service_with_non_exportable(tmp_path)
    service.select_all_exportable()
    service.select([non_exportable], allow_review=True)
    plan = service.generated_plan("demo", "Demo")
    report = describe_plan(plan, {non_exportable: "Aplicación sin repositorio"})
    assert not report.is_complete
    assert "OMITIDOS" in report.headline
    assert any(name == "Aplicación sin repositorio" for name, _reason in report.skipped)


def test_a_plan_without_operations_is_an_error_not_an_empty_package(tmp_path):
    service, non_exportable = _service_with_non_exportable(tmp_path)
    service.select([non_exportable], allow_review=True)
    with pytest.raises(ConstructorError):
        service.generated_plan("vacio", "Vacío")


def _pilot_app(tmp_path):
    from styler.tui.app import StylerApp
    return StylerApp(root=str(tmp_path / "library"), home=str(tmp_path / "home"))


def test_wizard_starts_on_the_first_unsatisfied_step(tmp_path):
    pytest.importorskip("textual")
    from styler.tui.app import ChangeConstructorScreen

    async def scenario():
        app = _pilot_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#nav-tools")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ChangeConstructorScreen)
            assert screen._step_key() == "baseline"
            assert not screen.query_one("#step-baseline").has_class("hidden")
            assert screen.query_one("#step-scan").has_class("hidden")
            assert screen.query_one("#step-selection").has_class("hidden")
            assert screen.query_one("#step-package").has_class("hidden")

    asyncio.run(scenario())


def test_continue_is_blocked_until_the_step_is_satisfied(tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import Button

    async def scenario():
        app = _pilot_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#nav-tools")
            await pilot.pause()
            primary = app.screen.query_one("#constructor-primary", Button)
            assert primary.disabled is True
            assert "línea base" in (primary.tooltip or "")

    asyncio.run(scenario())


def test_only_one_primary_button_is_visible(tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import Button

    async def scenario():
        app = _pilot_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#nav-tools")
            await pilot.pause()
            visible_primaries = [
                button for button in app.screen.query(Button)
                if button.variant == "primary" and button.display and button.region.width
            ]
            assert len(visible_primaries) == 1

    asyncio.run(scenario())


def test_every_visible_button_fits_in_80_columns(tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import Button

    async def scenario():
        app = _pilot_app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.click("#nav-tools")
            await pilot.pause()
            for button in app.screen.query(Button):
                if not button.display or not button.region.width:
                    continue
                assert button.region.right <= 80, f"{button.id} se sale de la pantalla"

    asyncio.run(scenario())


def test_repeated_refresh_does_not_duplicate_ids(tmp_path):
    pytest.importorskip("textual")

    async def scenario():
        app = _pilot_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#nav-tools")
            await pilot.pause()
            screen = app.screen
            for _ in range(5):
                await screen._refresh_baselines()
                await screen._refresh_packages()
                await pilot.pause()

    asyncio.run(scenario())


def test_no_free_text_path_inputs_remain():
    source = Path("styler/tui/app.py").read_text(encoding="utf-8")
    start = source.index("class ChangeConstructorScreen")
    end = source.index("class HistoryScreen")
    block = source[start:end]
    for obsolete in (
        "constructor-baseline-import-path",
        "constructor-package-import-path",
        "constructor-package-export-path",
    ):
        assert obsolete not in block
    assert "choose_portable_package_file" in source
    assert "choose_directory" in source
