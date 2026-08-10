"""Ayuda contextual para las tres áreas actuales de Styler."""
from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView, Static

from styler.tui.icons import action_label, icon, icon_label


@dataclass(frozen=True, slots=True)
class HelpEntry:
    key: str
    title: str
    summary: str
    example: str
    illustration: str


def _g(name: str, fallback: str) -> str:
    return icon(name) or fallback


def _illustrations() -> dict[str, str]:
    return {
        "home": (
            f"{_g('profile', '[PC]')} Equipo  →  {_g('review', '[REVISAR]')} Styler  →  "
            f"{_g('save', '[PAQUETE]')} cambio.stylerpkg"
        ),
        "changes": (
            "Cambios disponibles      →      Cambios en este equipo\n"
            "PhotoGIMP                       Sistema limpio"
        ),
        "constructor": (
            "✓ 1 Punto de partida  ▌2 Detección   3 Selección   4 Paquete"
        ),
        "report": (
            "Incluidos  ✓✓✓✓        Omitidos  ⚠⚠\n"
            "                        (con su motivo)"
        ),
        "baseline": (
            "Estado de referencia  →  comparación  →  diferencias detectadas"
        ),
        "selection": (
            "Cambios detectados          Contenido del paquete\n"
            "[x] Aplicación       →      Aplicación + tema + CSS"
        ),
        "plan": (
            "Receta semántica  →  DAG automático  →  verificación y retiro"
        ),
        "library": (
            f"{_g('library', '[BIBLIOTECA]')} Paquetes creados e importados"
        ),
        "history": (
            "Estado anterior   →   Estado actual\n"
            f"                 {_g('undo', '[VOLVER]')} Deshacer"
        ),
        "undo": (
            f"Estado actual   ←   {_g('undo', '[VOLVER]')}   ←   Estado anterior"
        ),
    }


def illustration(name: str) -> str:
    visuals = _illustrations()
    return visuals.get(name, visuals["home"])


ENTRIES: dict[str, HelpEntry] = {
    "home": HelpEntry(
        "home",
        "Qué hace Styler",
        "Integra cambios, registra actividad reversible y construye paquetes reutilizables.",
        "Construye un .stylerpkg con una aplicación y su apariencia.",
        "home",
    ),
    "changes": HelpEntry(
        "changes",
        "Cambios integrables",
        "Agrupa aplicaciones, archivos y pasos por la transformación que consiguen.",
        "PhotoGIMP resuelve GIMP, respaldo, integración y verificación como una unidad.",
        "changes",
    ),
    "changes-provider": HelpEntry(
        "changes-provider",
        "Fuente del requisito",
        "La fuente determina cómo se obtiene una aplicación sin dividir el cambio semántico.",
        "Flathub puede automatizar GIMP; otro proveedor puede exigir intervención.",
        "changes",
    ),
    "changes-progress": HelpEntry(
        "changes-progress",
        "Progreso por fases",
        "Muestra la fase actual, la salida en vivo y qué operación falta.",
        "Durante PhotoGIMP verás instalación, apertura, respaldo y verificación.",
        "changes",
    ),
    "constructor": HelpEntry(
        "constructor",
        "Constructor de cambios",
        "Reúne en un mismo flujo la línea base, detección, selección, plan y exportación.",
        "Detecta Stacer, un tema y un CSS; selecciónalos y genera un solo .stylerpkg.",
        "constructor",
    ),
    "constructor-baseline": HelpEntry(
        "constructor-baseline",
        "Punto de partida",
        "La línea base permite distinguir lo que ya existía de lo añadido o modificado después. Seleccionar una fila no la activa: usa «Usar esta» cuando quieras convertirla en el punto de comparación.",
        "Puedes seleccionar una línea base para exportarla o eliminarla sin cambiar la que está activa.",
        "baseline",
    ),
    "constructor-selection": HelpEntry(
        "constructor-selection",
        "Selección del paquete",
        "La izquierda muestra lo detectado y la derecha reúne lo que formará el paquete.",
        "Combina una AppImage, un cursor y un archivo CSS en el mismo cambio.",
        "selection",
    ),
    "constructor-plan": HelpEntry(
        "constructor-plan",
        "Plan automático",
        "Styler crea una receta semántica y la compila a un DAG seguro y revisable.",
        "Pulsa “Desglosar plan” para ver nodos, dependencias y verificaciones.",
        "plan",
    ),
    "constructor-report": HelpEntry(
        "constructor-report",
        "Qué entró y qué no",
        "El informe distingue lo que sí se convirtió en operaciones de lo que quedó fuera, con su motivo.",
        "Si un AppImage se movió de carpeta, aparecerá como omitido y no en el paquete.",
        "report",
    ),
    "constructor-saved": HelpEntry(
        "constructor-saved",
        "Paquetes guardados",
        "Administra borradores y paquetes importados sin salir del Constructor.",
        "Importa, exporta o elimina un .stylerpkg.",
        "library",
    ),
    "history": HelpEntry(
        "history",
        "Historial de cambios",
        "Ordena las operaciones aplicadas y señala cuáles siguen siendo reversibles.",
        "Selecciona una entrada para revisar o deshacer sus efectos.",
        "history",
    ),
    "history-undo": HelpEntry(
        "history-undo",
        "Deshacer",
        "Restaura las rutas que Styler modificó en esa operación.",
        "Vuelve al estado anterior sin borrar documentos personales.",
        "undo",
    ),
}


SCREEN_TOPICS: dict[str, tuple[str, ...]] = {
    "home": ("home",),
    "changes": ("changes", "changes-provider", "changes-progress"),
    "constructor": (
        "constructor",
        "constructor-baseline",
        "constructor-selection",
        "constructor-plan",
        "constructor-report",
        "constructor-saved",
    ),
    "history": ("history", "history-undo"),
}


class Illustration(Static):
    """Ilustración ASCII reutilizable."""

    def __init__(self, name: str, **kwargs) -> None:
        super().__init__(illustration(name), markup=False, **kwargs)
        self.illustration_name = name

    def show(self, name: str) -> None:
        self.illustration_name = name
        self.update(illustration(name))


class HelpModal(ModalScreen[None]):
    """Ayuda breve de la pantalla y de cada parte importante."""

    BINDINGS = [Binding("escape", "close", "Cerrar"), Binding("f1", "close", "Cerrar")]

    def __init__(self, screen_key: str, initial_key: str | None = None) -> None:
        super().__init__()
        self.screen_key = screen_key if screen_key in SCREEN_TOPICS else "home"
        self.keys = SCREEN_TOPICS[self.screen_key]
        self.initial_key = initial_key if initial_key in self.keys else self.keys[0]

    def compose(self) -> ComposeResult:
        with Container(classes="modal help-modal"):
            with Horizontal(classes="help-heading"):
                yield Label(icon_label("help", "Ayuda"), classes="modal-title")
                yield Static(
                    "Elige una parte de la pantalla para ver qué hace.",
                    classes="help-instruction",
                )
            with Horizontal(classes="help-layout"):
                with Vertical(classes="help-index"):
                    yield ListView(
                        *[self._topic_item(key) for key in self.keys],
                        id="help-topics",
                    )
                with VerticalScroll(classes="help-detail"):
                    yield Illustration("home", id="help-illustration", classes="help-illustration")
                    yield Label("", id="help-title", classes="help-title")
                    yield Static("", id="help-summary", classes="help-summary")
                    yield Label(icon_label("example", "Ejemplo"), classes="help-example-label")
                    yield Static("", id="help-example", classes="help-example")
            with Horizontal(classes="modal-actions"):
                yield Button(action_label("Entendido", "finish"), id="help-close", variant="primary")

    def _topic_item(self, key: str) -> ListItem:
        item = ListItem(Label(icon_label(ENTRIES[key].illustration, ENTRIES[key].title)))
        item.help_key = key  # type: ignore[attr-defined]
        return item

    def on_mount(self) -> None:
        listing = self.query_one("#help-topics", ListView)
        index = self.keys.index(self.initial_key)
        listing.index = index
        self._show(self.initial_key)

    def _show(self, key: str) -> None:
        entry = ENTRIES[key]
        self.query_one("#help-illustration", Illustration).show(entry.illustration)
        self.query_one("#help-title", Label).update(icon_label(entry.illustration, entry.title))
        self.query_one("#help-summary", Static).update(entry.summary)
        self.query_one("#help-example", Static).update(entry.example)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        key = getattr(event.item, "help_key", "") if event.item else ""
        if key in ENTRIES:
            self._show(key)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        key = getattr(event.item, "help_key", "")
        if key in ENTRIES:
            self._show(key)

    def action_close(self) -> None:
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "help-close":
            self.dismiss()
