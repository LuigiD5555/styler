"""Regresión: seleccionar una baseline no debe activarla."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from styler.baselines import BaselineKind, BaselineService
from tests.test_change_constructor_070 import _inventory


def test_source_separates_baseline_selection_from_activation():
    source = Path("styler/tui/app.py").read_text(encoding="utf-8")
    start = source.index("async def on_list_view_selected", source.index("class ChangeConstructorScreen"))
    end = source.index("async def _activate_baseline", start)
    block = source[start:end]
    baseline_branch = block[block.index("if isinstance(item, BaselineRow):"): block.index("if not isinstance(item, DetectedRow):")]
    assert "_activate_baseline" not in baseline_branch
    assert "self.focused_baseline = item" in baseline_branch
    assert "constructor-baseline-use" in source
    assert "await self._activate_baseline(self.focused_baseline.baseline_id)" in source


def test_constructor_has_explicit_use_baseline_action():
    source = Path("styler/tui/app.py").read_text(encoding="utf-8")
    assert 'action_label("Usar esta", "apply")' in source
    assert 'id="constructor-baseline-use"' in source
    assert "Seleccionada:" in source
    assert "todavía no activa" in source


def test_selecting_baseline_row_does_not_change_active_baseline(tmp_path):
    pytest.importorskip("textual")
    from styler.tui.app import ChangeConstructorScreen, StylerApp
    from styler.tui.constructor_widgets import BaselineRow

    root = tmp_path / "library"
    home = tmp_path / "home"
    home.mkdir(parents=True)
    baselines = BaselineService(root, home=home)
    first = baselines.register_inventory(
        _inventory("baseline-active"),
        kind=BaselineKind.CUSTOM,
        baseline_id="baseline-active",
        name="Base activa",
        activate_after=True,
    )
    second = baselines.register_inventory(
        _inventory("baseline-other"),
        kind=BaselineKind.CUSTOM,
        baseline_id="baseline-other",
        name="Base para exportar",
        activate_after=False,
    )

    async def scenario() -> None:
        app = StylerApp(root=str(root), home=str(home))
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#nav-tools")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ChangeConstructorScreen)
            await pilot.click("#constructor-back")
            await pilot.pause()
            rows = list(screen.query(BaselineRow))
            target = next(row for row in rows if row.baseline_id == second.baseline_id)
            await pilot.click(f"#{target.id}")
            await pilot.pause()
            assert screen.focused_baseline is not None
            assert screen.focused_baseline.baseline_id == second.baseline_id
            assert app.baselines.active(auto_select=False).baseline_id == first.baseline_id

            await pilot.click("#constructor-baseline-use")
            await pilot.pause()
            assert app.baselines.active(auto_select=False).baseline_id == second.baseline_id

    asyncio.run(scenario())
