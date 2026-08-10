from __future__ import annotations

from styler.component_catalog import bridge


def test_catalog_note_for_gimp():
    bridge.reset_cache()
    note = bridge.catalog_note_for("application.gimp")
    assert note is not None
    assert note.component_id == "app.gimp"
    assert note.rollback_level == "best_effort"
    assert "executable:gimp" in note.verification_checks


def test_catalog_note_for_kde():
    bridge.reset_cache()
    note = bridge.catalog_note_for("desktop.kde-plasma")
    assert note is not None
    assert note.component_id == "desktop.kde.plasma"


def test_catalog_note_for_konsole_usa_el_catalogo_canónico():
    bridge.reset_cache()
    note = bridge.catalog_note_for("application.konsole")
    assert note is not None
    assert note.component_id == "app.konsole"


def test_dependency_impact_incluye_nota_de_catalogo_para_gimp():
    from styler.models import Component
    from styler.component_graph import ComponentPlan
    from styler.ui.models import DependencyImpactItemView
    from styler.component_catalog.bridge import catalog_note_for

    gimp = Component(component_id="gimp", title="GIMP", category="aplicaciones", provides=["application.gimp"])
    note = catalog_note_for("application.gimp")
    assert note is not None
    item = DependencyImpactItemView(
        component_id=gimp.component_id,
        title=gimp.title,
        component_type=gimp.component_type,
        catalog_note=note.as_text(),
    )
    assert "rollback" in item.catalog_note
