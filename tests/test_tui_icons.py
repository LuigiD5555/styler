"""El vocabulario visual debe ayudar sin reemplazar el texto."""
from __future__ import annotations

from tempfile import TemporaryDirectory

import pytest


def test_modo_emoji_conserva_icono_y_etiqueta(monkeypatch):
    from styler.tui.icons import icon_label

    monkeypatch.setenv("STYLER_ICON_MODE", "emoji")
    value = icon_label("home", "Inicio")
    assert value.startswith("🏠")
    assert value.endswith("Inicio")


def test_modo_texto_no_deja_botones_sin_nombre(monkeypatch):
    from styler.tui.icons import action_label, icon_label

    monkeypatch.setenv("STYLER_ICON_MODE", "text")
    assert icon_label("library", "Biblioteca") == "Biblioteca"
    assert action_label("Deshacer el cambio") == "Deshacer el cambio"


def test_los_verbos_comunes_reciben_un_icono_semantico(monkeypatch):
    from styler.tui.icons import action_label

    monkeypatch.setenv("STYLER_ICON_MODE", "emoji")
    assert action_label("Importar").startswith("📥")
    assert action_label("Exportar").startswith("📤")
    assert action_label("Deshacer el cambio").startswith("↩")
    assert action_label("Eliminar").startswith("🗑")


def test_los_aliases_avanzados_mantienen_el_mismo_vocabulario(monkeypatch):
    from styler.tui.icons import icon

    monkeypatch.setenv("STYLER_ICON_MODE", "emoji")
    assert icon("scan-all") == icon("analyze")
    assert icon("policy") == icon("settings")
    assert icon("forget") == icon("delete")


@pytest.mark.asyncio
async def test_la_navegacion_con_emojis_cabe_en_ochenta_columnas(monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Button

    from styler.tui.app import StylerApp

    monkeypatch.setenv("STYLER_ICON_MODE", "emoji")
    with TemporaryDirectory() as root:
        app = StylerApp(root=root)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            expected = {
                "nav-changes": "Cambios",
                "nav-history": "Actividad",
                "nav-tools": "Herramientas",
            }
            for button_id, text in expected.items():
                button = app.screen.query_one(f"#{button_id}", Button)
                assert text in str(button.label)
                assert button.region.right <= 80
            assert app.screen.query_one("#help", Button).region.right <= 80
