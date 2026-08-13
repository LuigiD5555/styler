from pathlib import Path


def test_batch_rows_have_explicit_selected_visual_state():
    source = Path("styler/tui/app.py").read_text(encoding="utf-8")
    css = Path("styler/tui/styles/widgets.tcss").read_text(encoding="utf-8")

    assert 'classes += " batch-selected"' in source
    assert '"✓ SELECCIONADO"' in source
    assert 'id=f"batch-selected-badge-{safe}"' in source
    assert 'row.set_class(selected, "batch-selected")' in source
    assert 'badge.set_class(not selected, "hidden")' in source
    assert ".change-row.batch-selected" in css
    assert "border-left: thick $success;" in css
    assert ".change-batch-checkbox" not in css
    assert 'id=f"batch-select-{safe}"' not in source


def test_batch_counter_and_rows_share_same_selection_source():
    source = Path("styler/tui/app.py").read_text(encoding="utf-8")
    start = source.index("    def _render_batch_selection(self) -> None:")
    end = source.index("    def on_list_view_selected", start)
    block = source[start:end]

    assert "selected_ids = set(self.batch_selected_ids)" in block
    assert "row.card.change_id in selected_ids" in block
    assert "count = len(self.batch_selected_ids)" in block
    assert 'f"Integrar lote ({count})"' in block
    assert 'button = self.query_one("#integrate-change", Button)' in block


def test_clicking_available_row_toggles_integration_selection():
    source = Path("styler/tui/app.py").read_text(encoding="utf-8")
    start = source.index("    def on_list_view_selected(self, event: ListView.Selected) -> None:")
    end = source.index("    def _render_selected", start)
    block = source[start:end]

    assert 'if self.selected_side == "available":' in block
    assert "if change_id in self.batch_selected_ids:" in block
    assert "self.batch_selected_ids.append(change_id)" in block
