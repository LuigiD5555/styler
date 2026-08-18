"""Interfaz principal de Styler: Cambios, Actividad y Constructor de cambios."""
from __future__ import annotations

import asyncio
import shutil
from importlib.resources import files
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button, Footer, Input, Label, ListItem, ListView,
    ProgressBar, RadioButton, RadioSet, RichLog, Static,
)

from styler import __version__
from styler.baselines import BaselineService
from styler.changes import (
    AutomationLevel, ChangeBatchExecutionResult, ChangeBatchPlan, ChangeBatchProgressEvent,
    ChangeCard, ChangeExecutionResult, ChangePlan, ChangeProgressEvent, ChangeService,
    ChangeStatus, ProviderOption,
)
from styler.paths import default_export_directory, ensure_library_root
from styler.dialogs import choose_directory, choose_portable_package_file, native_dialog_available
from styler.portable import PackageType, PortableLibrary, inspect_package
from styler.privileges import authorize_sudo_interactive
from styler.execution.processes import ProcessRunner
from styler.services import UserError
from styler.tui.help import HelpModal, illustration
from styler.tui.icons import action_label, icon, icon_label
from styler.ui.activity import ActivityService
from styler.ui.errors import UserFacingError, to_user_error
from styler.ui.models import HistoryEntry
from styler.tui.constructor_widgets import BaselineRow, DetectedRow, SavedPackageRow
from styler.portable import normalize_identifier
from styler.ui.constructor import (
    ConstructorError, ChangeConstructorService, PlanReport, describe_plan,
)

RadioButton.BUTTON_LEFT = "("
RadioButton.BUTTON_INNER = "o"
RadioButton.BUTTON_RIGHT = ")"


def _load_tui_css() -> str:
    styles = files("styler.tui").joinpath("styles")
    return "\n".join(
        styles.joinpath(name).read_text(encoding="utf-8")
        for name in ("app.tcss", "screens.tcss", "widgets.tcss")
    )

class ErrorModal(ModalScreen[None]):
    """Nunca muestra un traceback. El detalle técnico está, pero no domina."""

    BINDINGS = [Binding("escape", "dismiss", "Cerrar")]

    def __init__(self, error: UserFacingError) -> None:
        super().__init__()
        self.error = error
        self._showing_detail = False

    def compose(self) -> ComposeResult:
        with Container(classes="modal"):
            yield Label(self.error.title, classes="modal-title")
            yield Static(self.error.message, classes="modal-body")
            if self.error.recovery:
                yield Static(self.error.recovery, classes="modal-recovery")
            yield Static("", id="technical", classes="technical hidden")
            with Horizontal(classes="modal-actions"):
                yield Button(action_label("Ver detalles técnicos", "details"), id="detail", classes="secondary")
                yield Button(action_label("Entendido", "finish"), id="close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss()
            return
        self._showing_detail = not self._showing_detail
        panel = self.query_one("#technical", Static)
        panel.update(
            f"[{self.error.technical_code}] {self.error.technical_detail}"
            if self._showing_detail
            else ""
        )
        panel.set_class(not self._showing_detail, "hidden")

class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    def __init__(
        self,
        title: str,
        body: str,
        confirm_label: str,
        danger: bool = False,
        cancel_label: str = "Cancelar",
    ) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._confirm_label = confirm_label   # nunca "OK": siempre la acción real
        self._danger = danger
        self._cancel_label = cancel_label

    def compose(self) -> ComposeResult:
        with Container(classes="modal danger" if self._danger else "modal"):
            yield Label(self._title, classes="modal-title")
            yield Static(self._body, classes="modal-body")
            with Horizontal(classes="modal-actions"):
                yield Button(
                    action_label(self._cancel_label, "cancel"),
                    id="cancel",
                    classes="secondary",
                )
                yield Button(
                    action_label(self._confirm_label),
                    id="confirm",
                    variant="error" if self._danger else "primary",
                )

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

class MoreActionsModal(ModalScreen[str | None]):
    """Menú «⋯»: guarda las acciones poco frecuentes fuera del camino principal."""

    BINDINGS = [Binding("escape", "cancel", "Cerrar")]

    def __init__(self, title: str, options: list[tuple[str, str, str]]) -> None:
        super().__init__()
        self._title = title
        self._options = options   # (id, etiqueta, explicación)

    def compose(self) -> ComposeResult:
        with Container(classes="modal tall"):
            yield Label(self._title, classes="modal-title")
            with VerticalScroll(classes="modal-scroll"):
                for option_id, label, description in self._options:
                    with Vertical(classes="option-card"):
                        yield Button(action_label(label, option_id), id=f"more-{option_id}", classes="secondary")
                        yield Static(description, classes="option-hint")
            with Horizontal(classes="modal-actions"):
                yield Button(action_label("Cerrar", "close"), id="more-cancel", classes="secondary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        chosen = (event.button.id or "").removeprefix("more-")
        self.dismiss(None if chosen == "cancel" else chosen)

class StylerScreen(Screen):
    """Base con navegación persistente y traducción de errores.

    La barra de navegación va anclada arriba y marca la sección actual. Antes
    convivía abajo con el pie de atajos y parecían dos barras repetidas.
    """

    route = ""
    help_screen = "home"
    help_targets: dict[str, str] = {}

    # Styler organiza la navegación por objetivos, no por subsistemas
    # internos. Apps, archivos y escritorio siguen existiendo debajo, pero la
    # persona trabaja con cambios semánticos integrables.
    NAVIGATION = (
        ("Cambios", "changes"),
        ("Actividad", "history"),
        ("Herramientas", "tools"),
    )
    NAV_TOOLTIPS = {
        "changes": "Cambios disponibles y cambios presentes en este equipo",
        "history": "Actividad, resultados y operaciones reversibles",
        "tools": "Línea base, detección, paquete y plan automático",
    }

    BINDINGS = [Binding("escape", "app.back", "Volver")]

    def compose_navigation(self) -> ComposeResult:
        """Barra única, anclada arriba y primera en el orden del teclado."""
        with Horizontal(classes="nav"):
            yield Static("STYLER", classes="nav-brand")
            for label, route in self.NAVIGATION:
                classes = "nav-item active" if route == self.route else "nav-item"
                yield Button(
                    icon_label(route, label),
                    id=f"nav-{route}",
                    classes=classes,
                    tooltip=self.NAV_TOOLTIPS.get(route, label),
                )
            yield Button(icon("help") or "?", id="help", classes="nav-help", tooltip="Ayuda de esta pantalla")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "help":
            event.stop()
            self.open_help()
            return
        if event.button.id and event.button.id.startswith("nav-"):
            event.stop()
            self.app.go(event.button.id.removeprefix("nav-"))

    def open_help(self, initial_key: str | None = None) -> None:
        """Abre una ayuda visual; no sustituye la pantalla ni pierde el progreso."""

        preferred = initial_key or getattr(self, "_last_help_key", None)
        self.app.push_screen(HelpModal(self.help_screen, preferred))

    def on_descendant_focus(self, event) -> None:
        """Recuerda el último control útil para que F1 explique justo esa parte."""

        widget_id = event.widget.id or ""
        if widget_id == "help":
            return
        key = self.help_targets.get(widget_id)
        if key:
            self._last_help_key = key

    def show_error(self, exc: Exception) -> None:
        self.app.push_screen(ErrorModal(to_user_error(exc)))

class ChangeRow(ListItem):
    """Una intención completa; nunca muestra rutas sueltas como si fueran cambios."""

    def __init__(self, card: ChangeCard, *, side: str, batch_selected: bool = False) -> None:
        safe = "".join(ch if ch.isalnum() else "-" for ch in card.change_id)
        classes = f"change-row {card.status}"
        if side == "available" and batch_selected:
            classes += " batch-selected"
        super().__init__(id=f"{side}-change-{safe}", classes=classes)
        self.card = card
        self.side = side
        self.batch_selected = batch_selected

    def compose(self) -> ComposeResult:
        automation = (
            "Automático"
            if self.card.automation_level == AutomationLevel.AUTOMATIC
            else "Asistido"
        )
        with Horizontal(classes="change-row-heading"):
            if self.side == "available":
                safe = "".join(ch if ch.isalnum() else "-" for ch in self.card.change_id)
                yield Static(
                    "✓ SELECCIONADO",
                    id=f"batch-selected-badge-{safe}",
                    classes=(
                        "batch-selected-badge"
                        if self.batch_selected
                        else "batch-selected-badge hidden"
                    ),
                )
            yield Static(self.card.name, classes="change-name")
        yield Static(
            f"{self.card.status_label} · {self.card.provider_label or automation}",
            classes="change-meta",
        )
        yield Static(self.card.category, classes="change-category")

class ProviderSettingsModal(ModalScreen[str | None]):
    """Configura la fuente de un requisito sin desarmar el cambio en la UI."""

    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    def __init__(
        self,
        change_name: str,
        options: tuple[ProviderOption, ...],
        selected: str,
    ) -> None:
        super().__init__()
        self.change_name = change_name
        self.options = options
        self.selected = selected
        self.by_id = {option.provider_id: option for option in options}

    def compose(self) -> ComposeResult:
        with Container(classes="modal tall provider-modal"):
            yield Label(f"Configurar {self.change_name}", classes="modal-title")
            yield Static(
                "El cambio sigue siendo una sola unidad. Esta opción únicamente decide "
                "cómo se obtiene su requisito GIMP.",
                classes="modal-body",
            )
            with VerticalScroll(classes="modal-scroll"):
                with RadioSet(id="change-provider-set"):
                    for option in self.options:
                        suffix = " · recomendada" if option.recommended else ""
                        if not option.available:
                            suffix += " · gestor no detectado"
                        yield RadioButton(
                            f"{option.label}{suffix}",
                            value=option.provider_id == self.selected,
                            id=f"change-provider-{option.provider_id}",
                        )
                yield Static("", id="provider-consequence", classes="provider-consequence")
            with Horizontal(classes="modal-actions"):
                yield Button(action_label("Cancelar", "cancel"), id="cancel", classes="secondary")
                yield Button(action_label("Guardar configuración", "save"), id="save", variant="primary")

    def on_mount(self) -> None:
        self._show_consequence(self.selected)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.pressed is None or not event.pressed.id:
            return
        provider_id = event.pressed.id.removeprefix("change-provider-")
        self._show_consequence(provider_id)

    def _show_consequence(self, provider_id: str) -> None:
        option = self.by_id.get(provider_id)
        if option is None:
            return
        if option.automation_level == AutomationLevel.AUTOMATIC:
            text = (
                option.description + "\n\n"
                "✓ Styler instalará GIMP, creará un respaldo, aplicará PhotoGIMP "
                "y verificará el resultado."
            )
        else:
            text = (
                option.description + "\n\n"
                "◐ Styler instalará GIMP y dejará PhotoGIMP en Descargas con "
                "instrucciones. No copiará archivos en rutas no validadas."
            )
        if option.warning:
            text += "\n\n⚠ " + option.warning
        self.query_one("#provider-consequence", Static).update(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id != "save":
            return
        selected = next(
            (
                option.id.removeprefix("change-provider-")
                for option in self.query(RadioButton)
                if option.value and option.id
            ),
            self.selected,
        )
        self.dismiss(selected)
