"""Pruebas de la capa de interfaz: navegación real, foco y tamaños.

Las pruebas que abren la TUI requieren Textual instalado. En un entorno sin
Textual se omiten, pero deben ejecutarse antes de publicar: la TUI ya es una
interfaz de usuario y hay que probarla ahora, no cuando exista una GUI.
"""
from __future__ import annotations

from tempfile import TemporaryDirectory

import pytest


# --------------------------------------------------------------------------- #
# Sin Textual: reglas que se pueden comprobar leyendo el código
# --------------------------------------------------------------------------- #

def test_la_barra_cabe_en_ochenta_columnas():
    pytest.importorskip("textual")
    from styler.tui.app import StylerScreen

    # 3 secciones × 12 columnas mínimas + marca + ayuda ≈ 47 < 80.
    assert len(StylerScreen.NAVIGATION) == 3
    ancho_estimado = 8 + len(StylerScreen.NAVIGATION) * 12 + 3
    assert ancho_estimado <= 80


def test_importar_ya_no_es_una_seccion_permanente():
    pytest.importorskip("textual")
    from styler.tui.app import StylerScreen

    rutas = {route for _label, route in StylerScreen.NAVIGATION}
    assert "import" not in rutas
    assert rutas == {"changes", "history", "tools"}


def test_ninguna_pantalla_reemplaza_metodos_reservados_de_textual():
    """0.8.3 definía `_render()` y Textual reventaba al dibujar."""
    pytest.importorskip("textual")
    from textual.screen import Screen

    from styler.tui import app as tui

    reservados = {"_render", "_render_content", "render", "render_line", "render_lines", "name"}
    culpables = [
        f"{name}: {sorted(set(vars(obj)) & reservados)}"
        for name, obj in vars(tui).items()
        if (
            isinstance(obj, type)
            and issubclass(obj, Screen)
            and obj.__module__ == tui.__name__
            and (set(vars(obj)) & reservados)
        )
    ]
    assert not culpables, "Pantallas que pisan atributos de Textual: " + "; ".join(culpables)


# --------------------------------------------------------------------------- #
# Con Textual: comportamiento real
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_cambiar_de_seccion_no_apila_pantallas():
    """El síntoma de «paneles repetidos» era una pila que crecía sin control."""
    pytest.importorskip("textual")
    from styler.tui.app import ChangeConstructorScreen, ChangesScreen, HistoryScreen, StylerApp

    with TemporaryDirectory() as root:
        app = StylerApp(root=root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            profundidad = len(app.screen_stack)
            assert isinstance(app.screen, ChangesScreen)

            for _ in range(3):
                app.go("tools")
                await pilot.pause()
                app.go("history")
                await pilot.pause()

            assert isinstance(app.screen, HistoryScreen)
            assert len(app.screen_stack) == profundidad  # no crece

            app.go("tools")
            await pilot.pause()
            assert isinstance(app.screen, ChangeConstructorScreen)
            assert len(app.screen_stack) == profundidad


@pytest.mark.asyncio
async def test_pulsar_dos_veces_la_misma_seccion_no_crea_copias():
    pytest.importorskip("textual")
    from styler.tui.app import ChangeConstructorScreen, StylerApp

    with TemporaryDirectory() as root:
        app = StylerApp(root=root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            profundidad = len(app.screen_stack)
            app.go("tools")
            await pilot.pause()
            app.go("tools")
            await pilot.pause()
            assert isinstance(app.screen, ChangeConstructorScreen)
            assert len(app.screen_stack) == profundidad


@pytest.mark.asyncio
async def test_escape_desde_una_seccion_regresa_a_inicio():
    pytest.importorskip("textual")
    from styler.tui.app import ChangesScreen, StylerApp

    with TemporaryDirectory() as root:
        app = StylerApp(root=root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.go("history")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ChangesScreen)


@pytest.mark.asyncio
async def test_la_navegacion_es_lo_primero_en_el_orden_del_teclado():
    """La barra se ve arriba: el tabulador debe encontrarla arriba también."""
    pytest.importorskip("textual")
    from textual.widgets import Button

    from styler.tui.app import StylerApp

    with TemporaryDirectory() as root:
        app = StylerApp(root=root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            botones = [
                widget for widget in app.screen.query(Button) if widget.id and widget.id.startswith("nav-")
            ]
            todos = list(app.screen.query(Button))
            assert botones, "La sección debe mostrar la barra de navegación"
            assert todos.index(botones[0]) == 0


@pytest.mark.asyncio
async def test_la_biblioteca_desactiva_sus_acciones_sin_seleccion():
    pytest.importorskip("textual")
    from textual.widgets import Button

    from styler.tui.app import ChangeConstructorScreen, StylerApp

    with TemporaryDirectory() as root:
        app = StylerApp(root=root)
        async with app.run_test(size=(100, 30)) as pilot:
            app.go("tools")
            await pilot.pause()
            assert isinstance(app.screen, ChangeConstructorScreen)
            for button_id in ("#constructor-back", "#constructor-baseline-use"):
                assert app.screen.query_one(button_id, Button).disabled is True


@pytest.mark.asyncio
async def test_el_historial_solo_activa_deshacer_cuando_es_reversible():
    pytest.importorskip("textual")
    from textual.widgets import Button

    from styler.tui.app import HistoryScreen, StylerApp

    with TemporaryDirectory() as root:
        app = StylerApp(root=root)
        async with app.run_test(size=(100, 30)) as pilot:
            app.go("history")
            await pilot.pause()
            assert isinstance(app.screen, HistoryScreen)
            assert app.screen.query_one("#undo", Button).disabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (100, 30), (120, 40)])
async def test_las_secciones_se_dibujan_en_los_tres_tamanos(size):
    """80×24 es un tamaño real, no una promesa de la documentación."""
    pytest.importorskip("textual")
    from styler.tui.app import StylerApp

    with TemporaryDirectory() as root:
        app = StylerApp(root=root)
        async with app.run_test(size=size) as pilot:
            for route in ("tools", "history", "changes"):
                app.go(route)
                await pilot.pause()
                assert app.screen.route == route


@pytest.mark.asyncio
async def test_el_boton_de_ayuda_abre_una_explicacion_visual():
    pytest.importorskip("textual")
    from styler.tui.app import StylerApp
    from styler.tui.help import HelpModal, Illustration

    with TemporaryDirectory() as root:
        app = StylerApp(root=root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.click("#help")
            await pilot.pause()
            assert isinstance(app.screen, HelpModal)
            assert app.screen.query_one("#help-illustration", Illustration).illustration_name

