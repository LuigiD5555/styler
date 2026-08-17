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
from styler.runtime.commands import PipeCraftRunner
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

class ChangesScreen(StylerScreen):
    """Pantalla principal: origen/destino y una sola acción de integración."""

    route = "changes"
    help_screen = "changes"
    AUTO_FOCUS = "#available-changes"

    def __init__(self) -> None:
        super().__init__()
        self.selected: ChangeCard | None = None
        self.selected_side = "available"
        self.batch_selected_ids: list[str] = []
        self._refresh_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        with Vertical(classes="page changes-page"):
            yield from self.compose_navigation()
            with Horizontal(classes="changes-heading"):
                with Vertical(classes="changes-heading-copy"):
                    yield Label("Integra cambios con significado", classes="page-title")
                    yield Static(
                        "Elige una transformación completa. Styler resolverá aplicaciones, "
                        "archivos, respaldos y pasos internos sin obligarte a separarlos.",
                        classes="tagline",
                    )
                yield Static("STYLER", classes="version-badge")

            with Horizontal(classes="changes-columns"):
                with Vertical(classes="change-column"):
                    yield Label("Cambios disponibles", classes="column-title")
                    yield Static(
                        "Transformaciones que Styler sabe preparar o integrar.",
                        classes="column-subtitle",
                    )
                    yield ListView(id="available-changes")
                with Vertical(classes="transfer-column"):
                    yield Static("→", classes="transfer-arrow", markup=False)
                    yield Static("Integrar", classes="transfer-label")
                with Vertical(classes="change-column"):
                    yield Label("Cambios en este equipo", classes="column-title")
                    yield Static(
                        "Estado detectado respecto de una instalación limpia.",
                        classes="column-subtitle",
                    )
                    yield ListView(id="integrated-changes")

            with Horizontal(classes="change-batch-bar"):
                yield Static(
                    "Haz clic en cualquier cambio para seleccionarlo. Uno se integra de forma individual; varios, como lote.",
                    id="batch-selection-summary",
                    classes="batch-selection-summary",
                )

            with Vertical(classes="change-detail-panel"):
                yield Label("Selecciona un cambio", id="change-detail-title", classes="detail-title")
                yield Static(
                    "Aquí verás qué consigue, qué estrategia utilizará y qué parte puede automatizarse.",
                    id="change-detail-body",
                    classes="detail-body",
                )
                yield Static("", id="change-detail-warning", classes="change-warning hidden")
                with Horizontal(classes="actions"):
                    yield Button(
                        action_label("Configurar", "settings"),
                        id="configure-change",
                        classes="secondary",
                        disabled=True,
                    )
                    yield Button(
                        action_label("Integrar cambio", "apply"),
                        id="integrate-change",
                        variant="primary",
                        disabled=True,
                    )
                    yield Button(
                        action_label("Eliminar disponible", "delete"),
                        id="delete-available-change",
                        classes="secondary hidden",
                        disabled=True,
                        tooltip="Eliminar de Styler la fuente de este cambio disponible",
                    )
                    yield Button(
                        action_label("Quitar cambio", "undo"),
                        id="remove-change",
                        classes="secondary hidden",
                        disabled=True,
                        tooltip="Revisar y ejecutar el DAG de retiro",
                    )
        yield Footer()

    async def on_mount(self) -> None:
        await self.refresh_changes()

    async def refresh_changes(self) -> None:
        """Reconstruye ambas listas después de terminar el desmontaje anterior.

        ``ListView.clear`` y ``append`` son operaciones asíncronas en Textual.
        Ignorar sus awaitables dejaba la fila anterior montada y producía
        ``DuplicateIds`` al guardar un proveedor.
        """
        async with self._refresh_lock:
            available = self.query_one("#available-changes", ListView)
            integrated = self.query_one("#integrated-changes", ListView)
            await available.clear()
            await integrated.clear()

            available_cards = self.app.changes.available_changes()
            live_ids = {card.change_id for card in available_cards}
            self.batch_selected_ids = [
                change_id for change_id in self.batch_selected_ids if change_id in live_ids
            ]
            available_rows = [
                ChangeRow(
                    card,
                    side="available",
                    batch_selected=card.change_id in self.batch_selected_ids,
                )
                for card in available_cards
            ]
            if available_rows:
                await available.extend(available_rows)

            current = self.app.changes.integrated_changes()
            if current:
                await integrated.extend(
                    [ChangeRow(card, side="integrated") for card in current]
                )
            else:
                await integrated.append(
                    ListItem(
                        Static(
                            "Sistema limpio\n\nNo se detectaron cambios integrados por Styler.",
                            classes="clean-state",
                        ),
                        id="integrated-clean-state",
                        disabled=True,
                    )
                )
            if available_rows:
                available.index = 0
            self._render_batch_selection()

    def _render_batch_selection(self) -> None:
        selected_ids = set(self.batch_selected_ids)
        available = self.query_one("#available-changes", ListView)
        for row in available.query(ChangeRow):
            selected = row.card.change_id in selected_ids
            row.batch_selected = selected
            row.set_class(selected, "batch-selected")
            for badge in row.query(".batch-selected-badge"):
                badge.set_class(not selected, "hidden")

        count = len(self.batch_selected_ids)
        summary = self.query_one("#batch-selection-summary", Static)
        button = self.query_one("#integrate-change", Button)
        if count > 1:
            summary.update(
                f"{count} cambios seleccionados · integración por lote. Styler los ejecutará uno por uno y reconciliará el estado entre DAGs."
            )
            button.label = action_label(f"Integrar lote ({count})", "apply")
        elif count == 1:
            summary.update("1 cambio seleccionado · integración individual.")
            selected_id = self.batch_selected_ids[0]
            selected_row = next(
                (row for row in available.query(ChangeRow) if row.card.change_id == selected_id),
                None,
            )
            if selected_row is not None:
                card = selected_row.card
                button.label = action_label(
                    "Continuar integración"
                    if card.continuation_available
                    else (
                        "Integrar automáticamente"
                        if card.automation_level == AutomationLevel.AUTOMATIC
                        else "Preparar integración"
                    ),
                    "apply",
                )
        else:
            summary.update(
                "Haz clic en cualquier cambio para seleccionarlo. Uno se integra de forma individual; varios, como lote."
            )

        can_integrate = count > 0 and self.selected_side == "available"
        button.disabled = not can_integrate
        button.set_class(not can_integrate, "hidden")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.item, ChangeRow):
            return
        self.selected = event.item.card
        self.selected_side = "integrated" if event.list_view.id == "integrated-changes" else "available"
        if self.selected_side == "available":
            change_id = event.item.card.change_id
            if change_id in self.batch_selected_ids:
                self.batch_selected_ids = [
                    item for item in self.batch_selected_ids if item != change_id
                ]
            else:
                self.batch_selected_ids.append(change_id)
        self._render_selected()
        self._render_batch_selection()

    def _render_selected(self) -> None:
        card = self.selected
        if card is None:
            return
        automation = (
            "Integración automática"
            if card.automation_level == AutomationLevel.AUTOMATIC
            else "Integración asistida"
        )
        body = (
            f"{card.description}\n\n"
            f"Método: {card.provider_label}\n"
            f"Estrategia: {automation}\n"
            f"Estado: {card.status_label}\n\n"
            f"{card.detail}"
        )
        if self.selected_side == "integrated":
            if self.app.changes.can_rollback(card.change_id):
                body += "\n\nRetiro disponible: Styler conserva recibos y respaldos para quitar este cambio."
            else:
                body += (
                    "\n\nRetiro automático no disponible: no existen efectos registrados suficientes "
                    "para modificar el equipo con seguridad."
                )
        self.query_one("#change-detail-title", Label).update(card.name)
        self.query_one("#change-detail-body", Static).update(body)
        warning = self.query_one("#change-detail-warning", Static)
        warning.update(("⚠ " + card.warning) if card.warning else "")
        warning.set_class(not bool(card.warning), "hidden")
        available = self.selected_side == "available"
        configurable = available and card.provider_id not in {"stylerpkg", "yaml"}
        integrable = available
        deletable = available and self.app.changes.can_delete_available(card.change_id)
        removable = self.selected_side == "integrated" and self.app.changes.can_rollback(card.change_id)
        configure = self.query_one("#configure-change", Button)
        integrate = self.query_one("#integrate-change", Button)
        delete_available = self.query_one("#delete-available-change", Button)
        remove = self.query_one("#remove-change", Button)
        configure.disabled = not configurable
        integrate.disabled = not integrable
        delete_available.disabled = not deletable
        remove.disabled = not removable
        configure.set_class(not configurable, "hidden")
        integrate.set_class(not integrable, "hidden")
        delete_available.set_class(not deletable, "hidden")
        remove.set_class(self.selected_side != "integrated", "hidden")
        self._render_batch_selection()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id not in {
            "configure-change",
            "integrate-change",
            "delete-available-change",
            "remove-change",
        }:
            super().on_button_pressed(event)
            return
        event.stop()
        if self.selected is None:
            return
        if button_id == "delete-available-change":
            change_id = self.selected.change_id
            change_name = self.selected.name

            def _deleted(confirmed: bool) -> None:
                if not confirmed:
                    return
                try:
                    removed = self.app.changes.delete_available_change(change_id)
                except (ValueError, UserError) as exc:
                    self.show_error(UserError(str(exc)))
                    return
                self.selected = None
                self.batch_selected_ids = [
                    item for item in self.batch_selected_ids if item != change_id
                ]
                self.query_one("#change-detail-title", Label).update("Selecciona un cambio")
                self.query_one("#change-detail-body", Static).update(
                    f"{removed} fue eliminado de Styler. No se deshicieron cambios ya aplicados al equipo."
                )
                self.query_one("#change-detail-warning", Static).set_class(True, "hidden")
                self.query_one("#configure-change", Button).set_class(True, "hidden")
                self.query_one("#integrate-change", Button).set_class(True, "hidden")
                self.query_one("#delete-available-change", Button).set_class(True, "hidden")
                self.run_worker(self.refresh_changes(), group="changes-refresh", exclusive=True)

            self.app.push_screen(
                ConfirmModal(
                    "Eliminar cambio disponible",
                    f"Se eliminará «{change_name}» de la biblioteca de Styler y dejará de aparecer en Cambios. "
                    "Esto no deshace nada que ya se haya aplicado al equipo.",
                    "Eliminar",
                    danger=True,
                ),
                _deleted,
            )
            return
        if button_id == "remove-change":
            try:
                plan = self.app.changes.build_removal_plan(self.selected.change_id)
            except (ValueError, UserError) as exc:
                self.show_error(UserError(str(exc)))
                return
            self.app.push_screen(ChangeReviewScreen(plan))
            return
        if button_id == "configure-change":
            options = self.app.changes.provider_options(self.selected.change_id)
            selected = self.app.changes.provider_for(self.selected.change_id)

            def _saved(provider_id: str | None) -> None:
                if not provider_id:
                    return
                try:
                    self.app.changes.set_provider(self.selected.change_id, provider_id)
                except ValueError as exc:
                    self.show_error(UserError(str(exc)))
                    return
                self.run_worker(
                    self.refresh_changes(),
                    group="changes-refresh",
                    exclusive=True,
                )

            self.app.push_screen(
                ProviderSettingsModal(self.selected.name, options, selected),
                _saved,
            )
            return
        if button_id == "integrate-change":
            if len(self.batch_selected_ids) > 1:
                try:
                    batch = self.app.changes.build_batch_plan(tuple(self.batch_selected_ids))
                except (ValueError, UserError) as exc:
                    self.show_error(UserError(str(exc)))
                    return
                self.app.push_screen(ChangeBatchReviewScreen(batch))
                return
            if len(self.batch_selected_ids) == 1:
                change_id = self.batch_selected_ids[0]
                try:
                    plan = self.app.changes.build_plan(change_id)
                except (ValueError, UserError) as exc:
                    self.show_error(UserError(str(exc)))
                    return
                self.app.push_screen(ChangeReviewScreen(plan))
                return
            return
        try:
            plan = self.app.changes.build_plan(self.selected.change_id)
        except (ValueError, UserError) as exc:
            self.show_error(UserError(str(exc)))
            return
        self.app.push_screen(ChangeReviewScreen(plan))

class ChangeReviewScreen(StylerScreen):
    """Traduce el DAG resuelto a consecuencias y fases comprensibles."""

    help_screen = "changes"
    BINDINGS = [Binding("escape", "app.back", "Volver")]

    def __init__(self, plan: ChangePlan) -> None:
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        with Vertical(classes="page change-review-page"):
            yield from self.compose_navigation()
            action = "Quitar" if self.plan.operation == "remove" else "Integrar"
            yield Label(f"{action} {self.plan.name}", classes="page-title")
            yield Static(self.plan.summary, classes="summary")
            if self.plan.notice:
                yield Static("⚠ " + self.plan.notice, classes="change-warning")
            yield Label(
                "Plan de retiro" if self.plan.operation == "remove" else "Plan de integración",
                classes="section-title",
            )
            with VerticalScroll(classes="phase-review-list"):
                for index, phase in enumerate(self.plan.phases, 1):
                    with Horizontal(classes="phase-review-row"):
                        yield Static(f"{index:02d}", classes="phase-number")
                        with Vertical(classes="phase-review-copy"):
                            yield Static(phase.label, classes="phase-name")
                            yield Static(phase.description, classes="phase-description")
                        yield Static(f"{phase.weight * 100:.0f} %", classes="phase-weight")
            with Horizontal(classes="actions"):
                yield Button(action_label("Volver", "back"), id="back", classes="secondary")
                yield Button(
                    action_label(
                        (
                            "Quitar ahora"
                            if self.plan.operation == "remove"
                            else (
                                "Continuar ahora"
                                if self.plan.continuation_mode
                                else ("Integrar ahora" if self.plan.automatic else "Preparar ahora")
                            )
                        ),
                        "undo" if self.plan.operation == "remove" else "apply",
                    ),
                    id="start-change",
                    variant="primary",
                )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()
            return
        if event.button.id == "start-change":
            if self.app.changes.plan_requires_admin(self.plan):
                with self.app.suspend():
                    authorization = authorize_sudo_interactive()
                if not authorization.ok:
                    message = authorization.message
                    if authorization.detail:
                        message += f"\n{authorization.detail}"
                    self.show_error(UserError(message))
                    return
            self.app.push_screen(ChangeProgressScreen(self.plan))
            return
        super().on_button_pressed(event)

class ChangeProgressScreen(Screen):
    """Progreso total, fase actual y actividad concreta siempre visibles."""

    BINDINGS = [Binding("ctrl+q", "app.quit", "Salir")]

    def __init__(self, plan: ChangePlan) -> None:
        super().__init__()
        self.plan = plan
        self.phase_states = {phase.phase_id: "pending" for phase in plan.phases}
        self._task: asyncio.Task | None = None
        self._last_console_message = ""
        self._last_command = ""

    def compose(self) -> ComposeResult:
        with Vertical(classes="page styler-progress-page"):
            yield Static("STYLER", classes="progress-brand")
            yield Label(
                f"{'Quitando' if self.plan.operation == 'remove' else 'Procesando'} {self.plan.name}",
                classes="page-title",
            )
            yield Static("Preparando el plan…", id="live-phase", classes="live-phase")
            yield Static("", id="live-operation", classes="live-operation")
            yield Static("0 % total", id="overall-percent", classes="progress-percent")
            yield ProgressBar(total=100, id="overall-progress")
            yield Static("Progreso de la fase", classes="phase-progress-label")
            yield ProgressBar(total=100, id="phase-progress")
            yield Static(
                "PipeCraft transmite en vivo cada comando, su salida y los periodos sin actividad.",
                id="progress-explanation",
                classes="reassurance",
            )
            yield Static("", id="progress-message", classes="progress-message")
            with Horizontal(id="change-observability"):
                yield Static(self._phase_text(), id="phase-status-list", classes="phase-status-list")
                with Vertical(id="change-process-details"):
                    yield Static("Preparando el primer nodo…", id="change-process-activity")
                    yield Static("", id="change-process-command")
                    yield RichLog(
                        id="change-live-log",
                        wrap=True,
                        highlight=False,
                        markup=False,
                        auto_scroll=True,
                    )
                    yield Static("", id="change-log-path")
        yield Footer()

    def on_mount(self) -> None:
        self.app.writing = True
        self.query_one("#change-live-log", RichLog).write(
            "PipeCraft preparó el plan y abrió el hilo de observabilidad."
        )
        self._task = asyncio.create_task(self._execute())

    async def _execute(self) -> None:
        try:
            if self.plan.operation == "remove":
                result = await asyncio.to_thread(
                    self.app.changes.rollback_change,
                    self.plan.change_id,
                    self._progress_from_thread,
                )
            else:
                result = await asyncio.to_thread(
                    self.app.changes.execute,
                    self.plan.change_id,
                    self.plan.provider_id,
                    self._progress_from_thread,
                    self.plan.options,
                )
        except Exception as exc:
            result = ChangeExecutionResult(
                change_id=self.plan.change_id,
                name=self.plan.name,
                ok=False,
                status=ChangeStatus.FAILED,
                title=f"No se pudo completar {self.plan.name}",
                message=str(exc),
                provider_id=self.plan.provider_id,
                provider_label=self.plan.provider_label,
                automation_level=self.plan.automation_level,
                operation=self.plan.operation,
            )
        self.app.writing = False
        self.app.push_screen(ChangeResultScreen(result))

    def _progress_from_thread(self, event: ChangeProgressEvent) -> None:
        self.app.call_from_thread(self._apply_progress, event)

    def _apply_progress(self, event: ChangeProgressEvent) -> None:
        for phase in self.plan.phases:
            if phase.phase_id == event.phase_id:
                self.phase_states[phase.phase_id] = event.status
                break
        self.query_one("#live-phase", Static).update(
            f"Fase {event.phase_index} de {event.phase_count} · {event.phase_label}"
        )
        self.query_one("#live-operation", Static).update(event.operation)
        percent = int(round(event.total_progress * 100))
        self.query_one("#overall-percent", Static).update(f"{percent} % total")
        self.query_one("#overall-progress", ProgressBar).update(progress=percent)
        phase_bar = self.query_one("#phase-progress", ProgressBar)
        if event.phase_progress is not None:
            phase_bar.update(progress=int(round(event.phase_progress * 100)))
        self.query_one("#progress-message", Static).update(event.message or event.operation)
        self.query_one("#phase-status-list", Static).update(self._phase_text())
        self._apply_console_event(event)

    def _apply_console_event(self, event: ChangeProgressEvent) -> None:
        log = self.query_one("#change-live-log", RichLog)
        activity = self.query_one("#change-process-activity", Static)
        command = self.query_one("#change-process-command", Static)
        log_path = self.query_one("#change-log-path", Static)

        if event.log_path:
            log_path.update(f"Log durable: {event.log_path}")
        if event.command:
            self._last_command = event.command
            command.update(f"$ {event.command}")

        if event.event_type == "command_started":
            log.write("")
            log.write(f"$ {event.command}")
            activity.update(event.operation)
            return
        if event.event_type == "command_spawned":
            activity.update(
                f"Proceso activo · PID {event.pid or '?'} · {event.elapsed_seconds:.1f} s"
            )
            return
        if event.event_type == "command_output":
            if event.terminal_line:
                log.write(event.terminal_line)
            activity.update(
                f"Proceso activo · {event.elapsed_seconds:.0f} s · salida recibida ahora"
            )
            return
        if event.event_type == "command_heartbeat":
            activity.update(event.message)
            return
        if event.event_type == "command_finished":
            symbol = "✓" if event.returncode == 0 else "✕"
            log.write(f"{symbol} {event.message}")
            activity.update(event.message)
            return

        # Los pasos internos (descarga HTTP, esperas, copias y verificaciones) no
        # ejecutan necesariamente un comando, pero forman parte del mismo hilo.
        message = event.operation or event.message
        if message and message != self._last_console_message:
            prefix = {"completed": "✓", "failed": "✕", "running": "→"}.get(
                event.status, "·"
            )
            log.write(f"{prefix} {message}")
            self._last_console_message = message
        activity.update(event.message or event.operation)

    def _phase_text(self) -> str:
        symbols = {
            "pending": "○",
            "running": "●",
            "completed": "✓",
            "failed": "✕",
        }
        lines = []
        for phase in self.plan.phases:
            state = self.phase_states.get(phase.phase_id, "pending")
            lines.append(f"{symbols.get(state, '○')} {phase.label}")
        return "\n".join(lines)

class ChangeResultScreen(Screen):
    BINDINGS = [Binding("escape", "finish", "Volver a cambios")]

    def __init__(self, result: ChangeExecutionResult) -> None:
        super().__init__()
        self.result = result

    def compose(self) -> ComposeResult:
        with Vertical(classes="page change-result-page"):
            yield Static("STYLER", classes="progress-brand")
            yield Label(
                ("✓ " if self.result.ok else "✕ ") + self.result.title,
                classes="page-title success" if self.result.ok else "page-title warning",
            )
            yield Static(self.result.message, classes="summary")
            if self.result.details:
                yield Static("\n".join(self.result.details), classes="result-details")
            if self.result.handoff_path:
                yield Static(
                    f"Archivo preparado:\n{self.result.handoff_path}",
                    classes="handoff-path",
                )
            if self.result.instructions_path:
                yield Static(
                    f"Instrucciones:\n{self.result.instructions_path}",
                    classes="handoff-path",
                )
            if self.result.diagnostic_path:
                yield Static(
                    f"Diagnóstico técnico guardado en:\n{self.result.diagnostic_path}",
                    classes="handoff-path",
                )
            with Horizontal(classes="actions"):
                if self.result.handoff_path:
                    yield Button(action_label("Abrir Descargas", "folder"), id="open-handoff", classes="secondary")
                if self.app.changes.can_rollback(self.result.change_id):
                    label = (
                        "Continuar retiro"
                        if self.result.operation == "remove"
                        else ("Revertir lo alcanzado" if not self.result.ok else "Quitar cambio")
                    )
                    yield Button(
                        action_label(label, "undo"),
                        id="undo",
                        classes="secondary",
                        tooltip="Revisar el DAG exacto antes de modificar el equipo",
                    )
                yield Button(action_label("Volver a cambios", "finish"), id="finish", variant="primary")
        yield Footer()

    def action_finish(self) -> None:
        self.app.go("changes")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "finish":
            self.action_finish()
            return
        if event.button.id == "open-handoff" and self.result.handoff_path:
            folder = str(Path(self.result.handoff_path).parent)
            if shutil.which("xdg-open"):
                PipeCraftRunner().spawn_detached(["xdg-open", folder])
            return
        if event.button.id == "undo":
            try:
                plan = self.app.changes.build_removal_plan(self.result.change_id)
            except ValueError as exc:
                self.app.push_screen(
                    ChangeResultScreen(
                        ChangeExecutionResult(
                            change_id=self.result.change_id,
                            name=self.result.name,
                            ok=False,
                            status=ChangeStatus.UNKNOWN,
                            title="No se puede construir el retiro",
                            message=str(exc),
                            provider_id=self.result.provider_id,
                            provider_label=self.result.provider_label,
                            automation_level=self.result.automation_level,
                            operation="remove",
                        )
                    )
                )
                return
            self.app.push_screen(ChangeReviewScreen(plan))


class ChangeBatchReviewScreen(StylerScreen):
    """Una sola revisión para varios DAG que seguirán ejecutándose aislados."""

    help_screen = "changes"
    BINDINGS = [Binding("escape", "app.back", "Volver")]

    def __init__(self, batch: ChangeBatchPlan) -> None:
        super().__init__()
        self.batch = batch

    def compose(self) -> ComposeResult:
        with Vertical(classes="page change-review-page batch-review-page"):
            yield from self.compose_navigation()
            yield Label(f"Integrar {self.batch.count} cambios", classes="page-title")
            yield Static(self.batch.summary, classes="summary")
            if self.batch.notice:
                yield Static("⚠ " + self.batch.notice, classes="change-warning")
            yield Label("Orden de ejecución", classes="section-title")
            with VerticalScroll(classes="phase-review-list batch-review-list"):
                for index, plan in enumerate(self.batch.plans, 1):
                    with Vertical(classes="batch-review-item"):
                        yield Static(
                            f"{index:02d} · {plan.name} · {plan.provider_label}",
                            classes="phase-name",
                        )
                        yield Static(plan.summary, classes="phase-description")
                        phases = " → ".join(phase.label for phase in plan.phases)
                        if phases:
                            yield Static(f"DAG: {phases}", classes="batch-dag-summary")
                        if plan.continuation_mode:
                            yield Static(
                                "Continuará desde los pasos pendientes.",
                                classes="change-meta",
                            )
            with Horizontal(classes="actions"):
                yield Button(action_label("Volver", "back"), id="back", classes="secondary")
                yield Button(
                    action_label(f"Integrar los {self.batch.count}", "apply"),
                    id="start-batch",
                    variant="primary",
                )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()
            return
        if event.button.id == "start-batch":
            if self.app.changes.batch_requires_admin(self.batch):
                with self.app.suspend():
                    authorization = authorize_sudo_interactive()
                if not authorization.ok:
                    message = authorization.message
                    if authorization.detail:
                        message += f"\n{authorization.detail}"
                    self.show_error(UserError(message))
                    return
            self.app.push_screen(ChangeBatchProgressScreen(self.batch))
            return
        super().on_button_pressed(event)


class ChangeBatchProgressScreen(Screen):
    """Ejecuta el lote secuencialmente sin fusionar los DAG individuales."""

    BINDINGS = [Binding("ctrl+q", "app.quit", "Salir")]

    def __init__(self, batch: ChangeBatchPlan) -> None:
        super().__init__()
        self.batch = batch
        self.states = {plan.change_id: "pending" for plan in batch.plans}
        self._task: asyncio.Task | None = None
        self._last_console_message = ""

    def compose(self) -> ComposeResult:
        with Vertical(classes="page styler-progress-page batch-progress-page"):
            yield Static("STYLER", classes="progress-brand")
            yield Label(f"Integrando {self.batch.count} cambios", classes="page-title")
            yield Static("Preparando el primer cambio…", id="batch-live-change", classes="live-phase")
            yield Static("0 % total", id="batch-overall-percent", classes="progress-percent")
            yield ProgressBar(total=100, id="batch-overall-progress")
            yield Static("Progreso del cambio actual", classes="phase-progress-label")
            yield ProgressBar(total=100, id="batch-change-progress")
            with Horizontal(id="change-observability"):
                yield Static(self._state_text(), id="batch-status-list", classes="phase-status-list")
                with Vertical(id="change-process-details"):
                    yield Static("Esperando a PipeCraft…", id="batch-process-activity")
                    yield Static("", id="batch-process-command")
                    yield RichLog(
                        id="batch-live-log",
                        wrap=True,
                        highlight=False,
                        markup=False,
                        auto_scroll=True,
                    )
                    yield Static("", id="batch-log-path")
        yield Footer()

    def on_mount(self) -> None:
        self.app.writing = True
        self.query_one("#batch-live-log", RichLog).write(
            "El lote mantendrá cada DAG aislado y reconstruirá el siguiente con el estado actualizado."
        )
        self._task = asyncio.create_task(self._execute())

    async def _execute(self) -> None:
        try:
            result = await asyncio.to_thread(
                self.app.changes.execute_batch,
                self.batch.change_ids,
                self._progress_from_thread,
            )
        except Exception as exc:
            # Última red de seguridad. No afirma que el lote nunca empezó: la
            # interfaz puede haber recibido progreso antes de una excepción
            # inesperada. Los errores de almacenamiento conocidos se convierten
            # en ChangeExecutionResult dentro de ChangeService.
            pending_ids = tuple(
                plan.change_id for plan in self.batch.plans
                if self.states.get(plan.change_id, "pending") == "pending"
            )
            pending_names = tuple(
                plan.name for plan in self.batch.plans
                if plan.change_id in pending_ids
            )
            had_activity = any(state != "pending" for state in self.states.values())
            result = ChangeBatchExecutionResult(
                change_ids=self.batch.change_ids,
                results=(),
                skipped_ids=pending_ids,
                skipped_names=pending_names,
                ok=False,
                title="El lote se interrumpió" if had_activity else "No se pudo iniciar el lote",
                message=str(exc),
            )
        self.app.writing = False
        self.app.push_screen(ChangeBatchResultScreen(result))

    def _progress_from_thread(self, event: ChangeBatchProgressEvent) -> None:
        self.app.call_from_thread(self._apply_progress, event)

    def _apply_progress(self, event: ChangeBatchProgressEvent) -> None:
        if event.status == "completed":
            self.states[event.change_id] = "completed"
        elif event.status == "failed":
            self.states[event.change_id] = "failed"
        else:
            self.states[event.change_id] = "running"
        self.query_one("#batch-live-change", Static).update(
            f"Cambio {event.change_index} de {event.change_count} · {event.change_name} · {event.phase_label}"
        )
        total = int(round(event.total_progress * 100))
        current = int(round(event.change_progress * 100))
        self.query_one("#batch-overall-percent", Static).update(f"{total} % total")
        self.query_one("#batch-overall-progress", ProgressBar).update(progress=total)
        self.query_one("#batch-change-progress", ProgressBar).update(progress=current)
        self.query_one("#batch-status-list", Static).update(self._state_text())
        self.query_one("#batch-process-activity", Static).update(event.message or event.operation)
        if event.command:
            self.query_one("#batch-process-command", Static).update(f"$ {event.command}")
        if event.log_path:
            self.query_one("#batch-log-path", Static).update(f"Log durable: {event.log_path}")
        self._apply_console_event(event)

    def _apply_console_event(self, event: ChangeBatchProgressEvent) -> None:
        log = self.query_one("#batch-live-log", RichLog)
        if event.event_type == "command_started":
            log.write("")
            log.write(f"[{event.change_name}] $ {event.command}")
            return
        if event.event_type == "command_output" and event.terminal_line:
            log.write(event.terminal_line)
            return
        if event.event_type == "command_finished":
            symbol = "✓" if event.returncode == 0 else "✕"
            log.write(f"{symbol} [{event.change_name}] {event.message}")
            return
        message = event.operation or event.message
        if message and message != self._last_console_message:
            prefix = {"completed": "✓", "failed": "✕", "running": "→"}.get(event.status, "·")
            log.write(f"{prefix} [{event.change_name}] {message}")
            self._last_console_message = message

    def _state_text(self) -> str:
        symbols = {"pending": "○", "running": "●", "completed": "✓", "failed": "✕"}
        return "\n".join(
            f"{symbols.get(self.states.get(plan.change_id, 'pending'), '○')} {plan.name}"
            for plan in self.batch.plans
        )


class ChangeBatchResultScreen(Screen):
    """Resultado agregado; los recibos siguen perteneciendo a cada cambio."""

    BINDINGS = [Binding("escape", "finish", "Volver a cambios")]

    def __init__(self, result: ChangeBatchExecutionResult) -> None:
        super().__init__()
        self.result = result

    def compose(self) -> ComposeResult:
        with Vertical(classes="page change-result-page batch-result-page"):
            yield Static("STYLER", classes="progress-brand")
            yield Label(
                ("✓ " if self.result.ok else "✕ ") + self.result.title,
                classes="page-title success" if self.result.ok else "page-title warning",
            )
            yield Static(self.result.message, classes="summary")
            with VerticalScroll(classes="batch-result-list"):
                for item in self.result.results:
                    yield Static(
                        ("✓ " if item.ok else "✕ ") + f"{item.name}: {item.message}",
                        classes="batch-result-item",
                    )
                    if not item.ok and item.details:
                        yield Static("\n".join(item.details), classes="result-details")
                    if not item.ok and item.diagnostic_path:
                        yield Static(
                            f"Diagnóstico: {item.diagnostic_path}",
                            classes="handoff-path",
                        )
                for name in self.result.skipped_names:
                    yield Static(
                        f"○ {name}: no se inició porque el lote se detuvo antes.",
                        classes="batch-result-item",
                    )
            yield Static(
                "Los cambios completados aparecen en ‘Cambios en este equipo’ y pueden retirarse individualmente.",
                classes="reassurance",
            )
            with Horizontal(classes="actions"):
                yield Button(action_label("Volver a cambios", "finish"), id="finish", variant="primary")
        yield Footer()

    def action_finish(self) -> None:
        self.app.go("changes")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "finish":
            self.action_finish()


class ChangeConstructorScreen(StylerScreen):
    """Asistente guiado: línea base → detección → selección → paquete."""

    # Las etiquetas de las filas son controles; no deben iniciar selección de
    # texto. Esto evita el fallo None.region de Textual al hacer clic dentro de
    # un ListView y no afecta la selección propia de Input/TextArea.
    ALLOW_SELECT = False

    MAX_VISIBLE_CHANGES = 500
    MAX_VISIBLE_PACKAGES = 200
    route = "tools"
    help_screen = "constructor"
    help_targets = {
        "constructor-baselines": "constructor-baseline",
        "constructor-detected": "constructor-selection",
        "constructor-selected": "constructor-selection",
        "constructor-plan-report": "constructor-report",
        "constructor-packages": "constructor-saved",
    }
    BINDINGS = [Binding("escape", "app.back", "Volver")]
    STEPS = (
        ("baseline", "1 Punto de partida"),
        ("scan", "2 Detección"),
        ("selection", "3 Selección"),
        ("package", "4 Paquete"),
    )

    def __init__(self, *, open_saved: bool = False) -> None:
        super().__init__()
        self.active_tab = "saved" if open_saved else "new"
        self.step_index = 0
        self.summary = None
        self.plan = None
        self.report: PlanReport | None = None
        self.scanning = False
        self.plan_details_visible = False
        self.focused_detected: DetectedRow | None = None
        self.focused_selected: DetectedRow | None = None
        self.focused_baseline: BaselineRow | None = None
        self.focused_package: SavedPackageRow | None = None
        self._refresh_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        with Vertical(classes="page constructor-page"):
            yield from self.compose_navigation()
            yield Label("Constructor de cambios", classes="page-title")
            with Horizontal(classes="actions constructor-tabs"):
                yield Button(action_label("Nuevo cambio", "start"), id="constructor-tab-new", classes="secondary active-tab")
                yield Button(action_label("Paquetes guardados", "library"), id="constructor-tab-saved", classes="secondary")

            with Vertical(id="constructor-new-view"):
                with Horizontal(id="constructor-chips", classes="constructor-chips"):
                    for key, label in self.STEPS:
                        yield Static(label, id=f"chip-{key}", classes="step-chip", markup=False)

                with Vertical(id="step-baseline", classes="constructor-step"):
                    yield Static(
                        "El punto de partida es lo que Styler considera «ya existía». "
                        "Sin él no puede afirmar qué es nuevo en tu equipo.",
                        classes="reassurance",
                    )
                    yield Static("", id="constructor-baseline-status", classes="stage", markup=False)
                    yield ListView(id="constructor-baselines")

                with Vertical(id="step-scan", classes="constructor-step hidden"):
                    yield Static(
                        "Styler revisará aplicaciones y recursos visuales. "
                        "No instala ni modifica nada en este paso.",
                        classes="reassurance",
                    )
                    yield ProgressBar(id="constructor-scan-progress", total=None, classes="hidden")
                    yield Static("", id="constructor-status", classes="detail", markup=False)

                with Vertical(id="step-selection", classes="constructor-step hidden"):
                    yield Static(
                        "Doble clic o Enter mueve un elemento entre las dos listas.",
                        classes="reassurance",
                    )
                    with Horizontal(classes="selection-columns"):
                        with Vertical():
                            yield Static("Cambios detectados", classes="section-title")
                            yield ListView(id="constructor-detected")
                        with Vertical():
                            yield Static("Contenido del paquete", classes="section-title")
                            yield ListView(id="constructor-selected")
                    yield Static("", id="constructor-detail", classes="detail", markup=False)

                with Vertical(id="step-package", classes="constructor-step hidden"):
                    yield Input(placeholder="ID del paquete (ej. mi-entorno)", id="constructor-package-id")
                    yield Input(placeholder="Nombre visible", id="constructor-package-name")
                    with Horizontal(classes="actions"):
                        yield Input(placeholder="Carpeta de destino", id="constructor-destination")
                        yield Button(action_label("Elegir carpeta", "folder"), id="constructor-pick-destination", classes="secondary")
                    yield Static("", id="constructor-plan-report", classes="plan-report", markup=False)
                    yield Static("", id="constructor-plan-details", classes="detail hidden", markup=False)

                with Horizontal(classes="actions step-actions"):
                    yield Button(action_label("Usar esta", "apply"), id="constructor-baseline-use", classes="secondary", disabled=True)
                    yield Button(action_label("Atrás", "back"), id="constructor-back", classes="secondary", disabled=True)
                    yield Button(action_label("Más", "more"), id="constructor-more", classes="secondary")
                    yield Button(action_label("Continuar", "next"), id="constructor-primary", variant="primary")

            with Vertical(id="constructor-saved-view", classes="hidden"):
                yield Static(
                    "Borradores y paquetes importados. Una línea base se administra en el paso 1.",
                    classes="reassurance",
                )
                yield ListView(id="constructor-packages")
                yield Static("", id="constructor-package-detail", classes="detail", markup=False)
                with Horizontal(classes="actions step-actions"):
                    yield Button(action_label("Más", "more"), id="constructor-saved-more", classes="secondary")
                    yield Button(action_label("Importar .stylerpkg", "import"), id="constructor-package-import", variant="primary")
        yield Footer()

    async def on_mount(self) -> None:
        self._show_tab(self.active_tab)
        self.summary = self.app.constructor.summary()
        await self._refresh_baselines()
        await self._refresh_lists()
        await self._refresh_packages()
        self._go_to_first_unsatisfied()
        self._render_step()

    def _show_tab(self, tab: str) -> None:
        self.active_tab = tab
        self.query_one("#constructor-new-view", Vertical).set_class(tab != "new", "hidden")
        self.query_one("#constructor-saved-view", Vertical).set_class(tab != "saved", "hidden")
        self.query_one("#constructor-tab-new", Button).set_class(tab == "new", "active-tab")
        self.query_one("#constructor-tab-saved", Button).set_class(tab == "saved", "active-tab")

    def _step_key(self) -> str:
        return self.STEPS[self.step_index][0]

    def _is_satisfied(self, key: str) -> bool:
        summary = self.summary
        if summary is None:
            return False
        if key == "baseline":
            return bool(summary.has_baseline)
        if key == "scan":
            return bool(summary.current_id)
        if key == "selection":
            return bool(summary.selected)
        if key == "package":
            return bool(self.plan and self.plan.included_ids)
        return False

    def _blocked_reason(self, key: str) -> str:
        return {
            "baseline": "Elige o captura una línea base para continuar.",
            "scan": "Ejecuta un escaneo para saber qué hay en este equipo.",
            "selection": "Añade al menos un elemento al contenido del paquete.",
            "package": "Genera el plan antes de crear el paquete.",
        }.get(key, "")

    def _go_to_first_unsatisfied(self) -> None:
        for index, (key, _label) in enumerate(self.STEPS):
            if not self._is_satisfied(key):
                self.step_index = index
                return
        self.step_index = len(self.STEPS) - 1

    def _render_step(self) -> None:
        current = self._step_key()
        for index, (key, label) in enumerate(self.STEPS):
            self.query_one(f"#step-{key}", Vertical).set_class(key != current, "hidden")
            chip = self.query_one(f"#chip-{key}", Static)
            chip.set_class(index < self.step_index and self._is_satisfied(key), "done")
            chip.set_class(key == current, "current")
            mark = "✓ " if (index < self.step_index and self._is_satisfied(key)) else ""
            chip.update(f"{mark}{label}")

        back = self.query_one("#constructor-back", Button)
        use_baseline = self.query_one("#constructor-baseline-use", Button)
        on_baseline = current == "baseline"
        back.set_class(on_baseline, "hidden")
        use_baseline.set_class(not on_baseline, "hidden")
        back.disabled = self.step_index == 0
        if on_baseline:
            active = self.app.baselines.active(auto_select=False)
            active_id = active.baseline_id if active else ""
            selected_id = self.focused_baseline.baseline_id if self.focused_baseline else ""
            use_baseline.disabled = not selected_id or selected_id == active_id
            if not selected_id:
                use_baseline.tooltip = "Selecciona una línea base de la lista."
            elif selected_id == active_id:
                use_baseline.tooltip = "Esta línea base ya está activa."
            else:
                use_baseline.tooltip = "Activa la línea base seleccionada como punto de comparación."
        primary = self.query_one("#constructor-primary", Button)
        satisfied = self._is_satisfied(current)
        if current == "scan" and not satisfied:
            primary.label = action_label("Escanear ahora", "analyze")
            primary.disabled = self.scanning
            primary.tooltip = None
        elif current == "package" and not satisfied:
            primary.label = action_label("Generar plan", "review")
            primary.disabled = False
            primary.tooltip = None
        elif current == "package":
            primary.label = action_label("Crear .stylerpkg", "save")
            primary.disabled = False
            primary.tooltip = None
        else:
            primary.label = action_label("Continuar", "next")
            primary.disabled = not satisfied
            primary.tooltip = None if satisfied else self._blocked_reason(current)

    async def _refresh_baselines(self) -> None:
        async with self._refresh_lock:
            listing = self.query_one("#constructor-baselines", ListView)
            await listing.clear()
            self.focused_baseline = None
            items = self.app.baselines.list()
            active = self.app.baselines.active(auto_select=False)
            active_id = active.baseline_id if active else ""
            if items:
                await listing.extend([BaselineRow(item, active=item.baseline_id == active_id) for item in items])
            else:
                await listing.append(ListItem(Static(
                    "No hay líneas base registradas. Usa «Más» para importar una, o captura el estado actual.",
                    markup=False,
                ), disabled=True))
            recommended = self.app.baselines.recommended()
            if active:
                text = f"Activa: {active.name}\n{self.app.baselines.system_label(active.system)}"
            elif recommended:
                text = f"Sin línea base activa. Compatible disponible: {recommended.name}"
            else:
                text = "Sin línea base activa ni oficial compatible."
            self.query_one("#constructor-baseline-status", Static).update(text)

    def _render_baseline_status(self) -> None:
        """Distingue claramente la línea seleccionada de la línea activa."""
        active = self.app.baselines.active(auto_select=False)
        recommended = self.app.baselines.recommended()
        lines: list[str] = []
        if active:
            lines.append(f"Activa: {active.name}")
            lines.append(self.app.baselines.system_label(active.system))
        elif recommended:
            lines.append(f"Sin línea base activa. Compatible disponible: {recommended.name}")
        else:
            lines.append("Sin línea base activa ni oficial compatible.")

        if self.focused_baseline is not None:
            selected = self.app.baselines.get(self.focused_baseline.baseline_id)
            if not active or selected.baseline_id != active.baseline_id:
                lines.append(f"Seleccionada: {selected.name} (todavía no activa)")
            else:
                lines.append(f"Seleccionada: {selected.name} (activa)")
        self.query_one("#constructor-baseline-status", Static).update("\n".join(lines))

    async def _refresh_lists(self) -> None:
        summary = self.summary
        if summary is None:
            return
        async with self._refresh_lock:
            detected = self.query_one("#constructor-detected", ListView)
            selected = self.query_one("#constructor-selected", ListView)
            await detected.clear()
            await selected.clear()
            self.focused_detected = None
            self.focused_selected = None
            chosen = set(summary.selected)
            pending = [item for item in summary.detected if item.change_id not in chosen]
            visible = pending[: self.MAX_VISIBLE_CHANGES]
            if visible:
                await detected.extend([DetectedRow(item) for item in visible])
            if not pending:
                await detected.append(ListItem(Static("Sin cambios pendientes.", markup=False), disabled=True))
            elif len(pending) > self.MAX_VISIBLE_CHANGES:
                await detected.append(ListItem(Static(
                    f"Mostrando {self.MAX_VISIBLE_CHANGES} de {len(pending)}. "
                    "Refina el registro o usa el inventario técnico para el resto.",
                    markup=False,
                ), disabled=True))
            by_id = {item.change_id: item for item in summary.detected}
            rows = [DetectedRow(by_id[cid], chosen=True) for cid in summary.selected if cid in by_id]
            if rows:
                await selected.extend(rows)
            else:
                await selected.append(ListItem(Static("El paquete está vacío.", markup=False), disabled=True))
            messages = [*summary.warnings, *summary.problems]
            if summary.inventory_only:
                messages.insert(0, "Modo inventario: sin línea base no se puede afirmar qué es nuevo.")
            self.query_one("#constructor-status", Static).update("\n".join(messages))

    async def _refresh_packages(self) -> None:
        async with self._refresh_lock:
            listing = self.query_one("#constructor-packages", ListView)
            await listing.clear()
            self.focused_package = None
            packages = self.app.portable_library.list_packages()
            if not packages:
                await listing.append(ListItem(Static("No hay paquetes guardados.", markup=False), disabled=True))
                return
            await listing.extend([SavedPackageRow(p) for p in packages[: self.MAX_VISIBLE_PACKAGES]])
            if len(packages) > self.MAX_VISIBLE_PACKAGES:
                await listing.append(ListItem(Static(
                    f"Mostrando {self.MAX_VISIBLE_PACKAGES} de {len(packages)} paquetes guardados.",
                    markup=False,
                ), disabled=True))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if isinstance(item, DetectedRow):
            if event.list_view.id == "constructor-detected":
                self.focused_detected = item
            else:
                self.focused_selected = item
            note = item.reason if not item.exportable else "Se empaquetará con respaldo y verificación."
            self.query_one("#constructor-detail", Static).update(f"{item.row_name}\n{note}")
        elif isinstance(item, BaselineRow):
            self.focused_baseline = item
            self._render_baseline_status()
            self._render_step()
        elif isinstance(item, SavedPackageRow):
            self.focused_package = item
            self.query_one("#constructor-package-detail", Static).update(f"{item.row_name}\n{item.row_meta}")

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, BaselineRow):
            # Seleccionar una fila no cambia el punto de comparación. La línea
            # base solo se activa mediante la acción explícita «Usar esta».
            self.focused_baseline = item
            self._render_baseline_status()
            self._render_step()
            return
        if not isinstance(item, DetectedRow):
            return
        try:
            if event.list_view.id == "constructor-detected":
                self.summary = self.app.constructor.select([item.change_id])
            else:
                self.summary = self.app.constructor.unselect([item.change_id])
        except Exception as exc:
            self.show_error(exc)
            return
        self.plan = None
        self.report = None
        await self._refresh_lists()
        self._render_step()

    async def _activate_baseline(self, baseline_id: str) -> None:
        try:
            self.app.baselines.activate(baseline_id)
            self.app.constructor.invalidate()
            self.summary = self.app.constructor.summary()
        except Exception as exc:
            self.show_error(exc)
            return
        await self._refresh_baselines()
        await self._refresh_lists()
        self._render_step()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "constructor-tab-new":
            self._show_tab("new")
            return
        if button_id == "constructor-tab-saved":
            self._show_tab("saved")
            return
        if button_id == "constructor-baseline-use":
            if self.focused_baseline is None:
                self.notify("Selecciona una línea base.", severity="warning")
                return
            await self._activate_baseline(self.focused_baseline.baseline_id)
            return
        if button_id == "constructor-back":
            self.step_index = max(0, self.step_index - 1)
            self._render_step()
            return
        if button_id == "constructor-primary":
            await self._primary_action()
            return
        if button_id == "constructor-more":
            self._open_step_menu()
            return
        if button_id == "constructor-saved-more":
            self._open_saved_menu()
            return
        if button_id == "constructor-pick-destination":
            self._pick_destination()
            return
        if button_id == "constructor-package-import":
            await self._import_saved_package()
            return
        super().on_button_pressed(event)

    async def _primary_action(self) -> None:
        key = self._step_key()
        if key == "scan" and not self._is_satisfied("scan"):
            self._start_scan()
            return
        if key == "package":
            if not self._is_satisfied("package"):
                self._generate_plan()
                return
            self.run_worker(self._export_change())
            return
        self.step_index = min(len(self.STEPS) - 1, self.step_index + 1)
        self._render_step()

    def _start_scan(self) -> None:
        self.scanning = True
        self.query_one("#constructor-scan-progress", ProgressBar).set_class(False, "hidden")
        self.query_one("#constructor-status", Static).update("Escaneando aplicaciones y recursos visuales…")
        self._render_step()
        self.run_worker(self._scan, thread=True, exclusive=True)

    def _scan(self) -> None:
        try:
            summary = self.app.constructor.refresh(scope="all")
        except Exception as exc:
            self.app.call_from_thread(self._scan_failed, exc)
            return
        self.app.call_from_thread(self._scan_finished, summary)

    def _scan_failed(self, exc: Exception) -> None:
        self.scanning = False
        self.query_one("#constructor-scan-progress", ProgressBar).set_class(True, "hidden")
        self.show_error(exc)
        self._render_step()

    def _scan_finished(self, summary) -> None:
        self.scanning = False
        self.summary = summary
        self.plan = None
        self.report = None
        self.query_one("#constructor-scan-progress", ProgressBar).set_class(True, "hidden")
        self.run_worker(self._after_scan())

    async def _after_scan(self) -> None:
        await self._refresh_lists()
        exportable = self.summary.exportable_count if self.summary else 0
        total = len(self.summary.detected) if self.summary else 0
        self.query_one("#constructor-status", Static).update(
            f"{total} elementos encontrados · {exportable} listos para empaquetar"
        )
        self._render_step()

    def _package_fields(self) -> tuple[str, str, Path]:
        id_input = self.query_one("#constructor-package-id", Input)
        name_input = self.query_one("#constructor-package-name", Input)
        raw_id = id_input.value.strip()
        name = name_input.value.strip()
        if not raw_id and not name:
            raise ConstructorError("Escribe un nombre para el paquete.")
        package_id = normalize_identifier(raw_id or name, fallback="change")
        # El identificador es técnico; el usuario puede escribir texto humano y
        # Styler muestra inmediatamente la forma segura que realmente usará.
        if id_input.value != package_id:
            id_input.value = package_id
        if not name:
            name = raw_id.strip() or package_id
            name_input.value = name
        raw = self.query_one("#constructor-destination", Input).value.strip()
        base = Path(raw).expanduser() if raw else default_export_directory()
        destination = base / f"{package_id}.stylerpkg" if base.is_dir() else base
        return package_id, name, destination

    def _pick_destination(self) -> None:
        if not native_dialog_available():
            self.notify("No hay selector gráfico; escribe la carpeta a mano.", severity="warning")
            self.query_one("#constructor-destination", Input).focus()
            return
        chosen = choose_directory(default_export_directory())
        if chosen:
            self.query_one("#constructor-destination", Input).value = chosen

    def _generate_plan(self) -> None:
        try:
            package_id, name, _destination = self._package_fields()
            plan = self.app.constructor.generated_plan(package_id, name)
        except Exception as exc:
            self.show_error(exc)
            return
        names = {item.change_id: item.name for item in (self.summary.detected if self.summary else ())}
        self.plan = plan
        self.report = describe_plan(plan, names)
        self._render_report()
        self._render_step()

    def _render_report(self) -> None:
        report = self.report
        target = self.query_one("#constructor-plan-report", Static)
        if report is None:
            target.update("Todavía no se ha generado un plan.")
            return
        lines = [
            report.headline,
            f"{report.operations} pasos · {report.assets} archivos incorporados",
            "Punto de recuperación y verificación final incluidos",
        ]
        if report.skipped:
            lines.extend(["", "OMITIDOS (no estarán en el paquete):"])
            lines.extend(f"  · {name}: {reason}" for name, reason in report.skipped)
        if report.warnings:
            lines.append("")
            lines.extend(f"  ! {item}" for item in report.warnings)
        target.update("\n".join(lines))

    async def _export_change(self) -> None:
        if self.report is not None and not self.report.is_complete:
            confirmed = await self.app.push_screen_wait(ConfirmModal(
                "Se omitirán elementos",
                f"{len(self.report.skipped)} elemento(s) seleccionados no pudieron convertirse "
                "en operaciones y no estarán en el paquete.",
                "Crear de todas formas",
            ))
            if not confirmed:
                return
        try:
            package_id, name, destination = self._package_fields()
            result = self.app.constructor.build_package(
                destination, package_id=package_id, name=name, plan=self.plan,
            )
            self.app.portable_library.import_package(result.path, collision_policy="replace_explicitly")
        except Exception as exc:
            self.show_error(exc)
            return
        detail = [
            f"Paquete creado: {result.path}",
            f"Elementos incluidos: {len(result.component_ids)}",
            "Ya está disponible en Cambios para revisarlo e integrarlo.",
        ]
        if result.skipped:
            detail.append(f"Elementos omitidos: {len(result.skipped)}")
        self.notify("\n".join(detail), timeout=10)
        await self._reset_after_export()

    async def _reset_after_export(self) -> None:
        """Cierra el ciclo terminado y vuelve a Detección con la misma baseline."""
        self.summary = self.app.constructor.begin_next_cycle()
        self.plan = None
        self.report = None
        self.plan_details_visible = False
        self.focused_detected = None
        self.focused_selected = None
        self.step_index = 1  # La baseline sigue activa; el siguiente trabajo empieza escaneando.

        self.query_one("#constructor-package-id", Input).value = ""
        self.query_one("#constructor-package-name", Input).value = ""
        self.query_one("#constructor-plan-report", Static).update("")
        details = self.query_one("#constructor-plan-details", Static)
        details.update("")
        details.set_class(True, "hidden")

        await self._refresh_lists()
        await self._refresh_packages()
        self._render_step()

    def _open_step_menu(self) -> None:
        key = self._step_key()
        options: list[tuple[str, str, str]] = []
        if key == "baseline":
            options = [
                ("baseline-recommended", "Usar oficial compatible", "Activa la línea base oficial que coincide con este sistema."),
                ("baseline-capture", "Capturar estado actual", "Toma el estado de hoy como punto de partida."),
                ("baseline-import", "Importar línea base", "Abre un .stylerpkg de tipo línea base."),
                ("baseline-export", "Exportar seleccionada", "Guarda la línea base elegida como .stylerpkg."),
                ("baseline-export-catalog", "Preparar para catálogo oficial", "Crea un .stylerpkg oficial para incluirlo en una versión futura."),
                ("baseline-delete", "Eliminar personalizada", "Borra el punto de comparación; no desinstala nada."),
                ("baseline-repair", "Reparar oficiales", "Revisa y restaura el catálogo oficial."),
            ]
        elif key == "scan":
            options = [
                ("scan-again", "Volver a escanear", "Repite la detección desde cero."),
                ("inventory", "Ver inventario técnico", "Muestra los identificadores tal como los guarda Styler."),
            ]
        elif key == "selection":
            options = [
                ("select-all", "Añadir todo lo exportable", "Añade los elementos que ya pueden empaquetarse."),
                ("clear", "Vaciar selección", "Deja el contenido del paquete en blanco."),
            ]
        elif key == "package":
            options = [
                ("plan-details", "Ver detalle del plan", "Muestra nodos, dependencias y verificaciones."),
                ("plan-again", "Regenerar plan", "Vuelve a sintetizar la receta y el DAG."),
            ]
        self.app.push_screen(MoreActionsModal("Más acciones", options), self._step_menu_chosen)

    def _step_menu_chosen(self, chosen: str | None) -> None:
        if chosen is not None:
            self.run_worker(self._run_menu_action(chosen))

    async def _run_menu_action(self, chosen: str) -> None:
        try:
            if chosen == "baseline-recommended":
                self.app.baselines.activate_recommended()
                self.app.constructor.invalidate()
                self.summary = self.app.constructor.summary()
                await self._refresh_baselines()
                await self._refresh_lists()
            elif chosen == "baseline-capture":
                self.summary = self.app.constructor.capture_baseline(name="Estado inicial personalizado")
                await self._refresh_baselines()
                await self._refresh_lists()
            elif chosen == "baseline-import":
                await self._import_baseline()
            elif chosen == "baseline-export":
                self._export_baseline()
            elif chosen == "baseline-export-catalog":
                await self._export_baseline_for_catalog()
            elif chosen == "baseline-delete":
                self._confirm_delete_baseline()
            elif chosen == "baseline-repair":
                repaired = self.app.baselines.repair_catalog()
                self.notify(f"Catálogo revisado; {len(repaired)} línea(s) reparadas.")
                await self._refresh_baselines()
            elif chosen == "scan-again":
                self._start_scan()
            elif chosen == "inventory":
                summary = self.summary
                self.query_one("#constructor-status", Static).update(
                    f"Inventario {summary.current_id or 'sin capturar'} · "
                    f"{len(summary.detected) if summary else 0} elementos."
                )
            elif chosen == "select-all":
                self.summary = self.app.constructor.select_all_exportable()
                self.plan = None
                self.report = None
                await self._refresh_lists()
            elif chosen == "clear":
                self.summary = self.app.constructor.clear_selection()
                self.plan = None
                self.report = None
                await self._refresh_lists()
            elif chosen == "plan-details":
                self._toggle_plan_details()
            elif chosen == "plan-again":
                self._generate_plan()
        except Exception as exc:
            self.show_error(exc)
            return
        self._render_step()

    def _toggle_plan_details(self) -> None:
        if self.plan is None:
            self.notify("Genera el plan primero.", severity="warning")
            return
        self.plan_details_visible = not self.plan_details_visible
        details = self.query_one("#constructor-plan-details", Static)
        details.update("\n".join(self.plan.details))
        details.set_class(not self.plan_details_visible, "hidden")

    def _open_saved_menu(self) -> None:
        options = [
            ("package-export", "Exportar seleccionado", "Guarda una copia .stylerpkg."),
            ("package-delete", "Eliminar seleccionado", "Quita el paquete de la biblioteca local."),
            ("packages-refresh", "Actualizar lista", "Vuelve a leer la biblioteca."),
        ]
        self.app.push_screen(MoreActionsModal("Paquetes guardados", options), self._saved_menu_chosen)

    def _saved_menu_chosen(self, chosen: str | None) -> None:
        if chosen is not None:
            self.run_worker(self._run_saved_action(chosen))

    async def _run_saved_action(self, chosen: str) -> None:
        if chosen == "packages-refresh":
            await self._refresh_packages()
            return
        if self.focused_package is None:
            self.notify("Selecciona un paquete de la lista.", severity="warning")
            return
        target = self.focused_package
        try:
            if chosen == "package-export":
                destination = default_export_directory()
                if native_dialog_available():
                    chosen_dir = choose_directory(destination)
                    if chosen_dir:
                        destination = Path(chosen_dir)
                path = self.app.portable_library.export(
                    target.package_id, destination, target.package_version,
                )
                self.notify(f"Paquete exportado: {path}")
            elif chosen == "package-delete":
                confirmed = await self.app.push_screen_wait(ConfirmModal(
                    "Eliminar paquete guardado",
                    f"Se quitará «{target.row_name}» de la biblioteca local. "
                    "No se desinstalará ninguna aplicación.",
                    "Eliminar",
                    danger=True,
                ))
                if not confirmed:
                    return
                self.app.portable_library.remove(target.package_id, target.package_version)
        except Exception as exc:
            self.show_error(exc)
            return
        await self._refresh_packages()

    async def _import_baseline(self) -> None:
        path = self._ask_for_package()
        if path is None:
            return
        try:
            self.app.baselines.import_package(path, activate_after=True)
            self.app.constructor.invalidate()
            self.summary = self.app.constructor.summary()
        except Exception as exc:
            self.show_error(exc)
            return
        await self._refresh_baselines()
        await self._refresh_lists()

    def _export_baseline(self) -> None:
        if self.focused_baseline is None:
            self.notify("Selecciona una línea base.", severity="warning")
            return
        destination = default_export_directory()
        if native_dialog_available():
            chosen_dir = choose_directory(destination)
            if not chosen_dir:
                return
            destination = Path(chosen_dir)
        target = destination / f"baseline-{self.focused_baseline.baseline_id}.stylerpkg"
        try:
            path = self.app.baselines.export_package(self.focused_baseline.baseline_id, target)
        except Exception as exc:
            self.show_error(exc)
            return
        self.notify(f"Línea base exportada: {path}")

    async def _export_baseline_for_catalog(self) -> None:
        """Exporta una candidata oficial sin convertir la copia local."""
        if self.focused_baseline is None:
            self.notify("Selecciona una línea base.", severity="warning")
            return
        confirmed = await self.app.push_screen_wait(ConfirmModal(
            "Preparar línea base oficial",
            "Solo procede si esta línea base fue capturada en una instalación limpia "
            "de la distribución indicada, antes de añadir aplicaciones o ajustes personales. "
            "La copia local seguirá siendo personalizada.",
            "Confirmar instalación limpia",
        ))
        if not confirmed:
            return
        destination = default_export_directory()
        if native_dialog_available():
            chosen_dir = choose_directory(destination)
            if not chosen_dir:
                return
            destination = Path(chosen_dir)
        try:
            path = self.app.baselines.export_catalog_candidate(
                self.focused_baseline.baseline_id,
                destination,
                clean_install_confirmed=True,
            )
        except Exception as exc:
            self.show_error(exc)
            return
        self.notify(f"Candidata oficial exportada: {path}", timeout=12)

    async def _import_saved_package(self) -> None:
        path = self._ask_for_package()
        if path is None:
            return
        try:
            inspection = inspect_package(path)
            if inspection.manifest.package_type is PackageType.BASELINE:
                self.app.baselines.import_package(path, activate_after=False)
                self.app.constructor.invalidate()
                self.summary = self.app.constructor.summary()
                await self._refresh_baselines()
                self.notify("Se importó como línea base; búscala en el paso 1.")
            else:
                package = self.app.portable_library.import_package(
                    path, collision_policy="replace_explicitly"
                )
                await self._refresh_packages()
                self.notify(
                    f"{package.manifest.name} ya está disponible en Cambios.", timeout=8
                )
        except Exception as exc:
            self.show_error(exc)

    def _ask_for_package(self) -> Path | None:
        if not native_dialog_available():
            self.notify(
                "No hay kdialog ni zenity. Usa «styler package import <ruta>» desde la terminal.",
                severity="warning",
                timeout=10,
            )
            return None
        chosen = choose_portable_package_file(Path.home())
        if not chosen:
            return None
        path = Path(chosen).expanduser()
        if not path.is_file():
            self.notify("Ese archivo ya no existe.", severity="error")
            return None
        return path

    def _confirm_delete_baseline(self) -> None:
        if self.focused_baseline is None:
            self.notify("Selecciona una línea base personalizada.", severity="warning")
            return
        baseline_id = self.focused_baseline.baseline_id
        try:
            definition = self.app.baselines.get(baseline_id)
        except Exception as exc:
            self.show_error(exc)
            return
        if definition.is_official:
            self.notify("Las líneas base oficiales se reparan; no se eliminan.", severity="warning")
            return

        def decided(confirmed: bool) -> None:
            if not confirmed:
                return
            try:
                self.app.constructor.delete_custom_baseline(baseline_id)
                self.summary = self.app.constructor.summary()
            except Exception as exc:
                self.show_error(exc)
                return
            self.run_worker(self._after_delete_baseline())

        self.app.push_screen(ConfirmModal(
            "Eliminar línea base personalizada",
            "Se eliminará el punto de comparación. No se desinstalará nada ni se modificarán aplicaciones.",
            "Eliminar",
            danger=True,
        ), decided)

    async def _after_delete_baseline(self) -> None:
        await self._refresh_baselines()
        await self._refresh_lists()
        self._render_step()

class HistoryScreen(StylerScreen):
    """El registro es de la persona, no de Styler.

    «Deshacer» es la acción principal y solo se activa cuando la entrada
    seleccionada es reversible. «Quitar del registro» y «Vaciar registro» son
    excepcionales: viven en «⋯», no junto a la acción principal.
    """

    route = "history"
    help_screen = "history"
    AUTO_FOCUS = "#history"
    help_targets = {"history": "history", "undo": "history-undo", "more": "history"}

    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[str, HistoryEntry] = {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="page"):
            yield from self.compose_navigation()
            yield Label(icon_label("history", "Historial"), classes="page-title")
            yield Static(
                "Cada línea es algo que Styler aplicó en tu escritorio.",
                classes="reassurance",
            )
            yield ListView(id="history")
            yield Static("", id="history-empty", classes="empty-state hidden")
            yield Static("", id="history-detail", classes="detail hidden", markup=False)
            with Horizontal(id="history-actions", classes="actions hidden"):
                yield Button(action_label("Más", "more"), id="more", classes="secondary", disabled=True)
                yield Button(action_label("Deshacer", "undo"), id="undo", variant="primary", disabled=True, tooltip="Deshacer este cambio")
        yield Footer()

    def on_mount(self) -> None:
        self._reload()

    # -- datos ---------------------------------------------------------

    def _reload(self) -> None:
        listing = self.query_one("#history", ListView)
        listing.clear()
        empty = self.query_one("#history-empty", Static)

        entries = self.app.activity.history()
        self.entries = {entry.transaction_id: entry for entry in entries}

        if not entries:
            listing.add_class("hidden")
            empty.remove_class("hidden")
            empty.update(
                illustration("empty-history")
                + "\n\nAplica una configuración para verla aquí."
            )
            self.query_one("#history-detail", Static).add_class("hidden")
            self.query_one("#history-actions", Horizontal).add_class("hidden")
            self.query_one("#more", Button).disabled = True
            self.query_one("#undo", Button).disabled = True
            return

        listing.remove_class("hidden")
        empty.add_class("hidden")
        self.query_one("#history-detail", Static).remove_class("hidden")
        self.query_one("#history-actions", Horizontal).remove_class("hidden")
        self.query_one("#more", Button).disabled = False
        for entry in entries:
            state = "● Aplicado" if entry.can_undo else f"○ {entry.outcome}"
            item = ListItem(
                Label(f"{state} — {entry.change_name}", classes="history-name"),
                Static(
                    f"{entry.when} · {entry.file_count} archivos · "
                    + ("se puede deshacer" if entry.can_undo else "ya no es reversible"),
                    classes="history-meta",
                ),
                classes="history-row undoable" if entry.can_undo else "history-row",
            )
            item.styler_transaction_id = entry.transaction_id  # type: ignore[attr-defined]
            listing.append(item)
        self._refresh_selection()

    def _selected(self) -> HistoryEntry | None:
        item = self.query_one("#history", ListView).highlighted_child
        transaction_id = getattr(item, "styler_transaction_id", "") if item else ""
        return self.entries.get(transaction_id)

    def _refresh_selection(self) -> None:
        entry = self._selected()
        undo = self.query_one("#undo", Button)
        detail = self.query_one("#history-detail", Static)

        if entry is None:
            undo.disabled = True
            detail.update(
                illustration("history")
                + "\n\nSelecciona una entrada para ver qué cambió."
            )
            return

        undo.disabled = not entry.can_undo
        lines = [
            illustration("undo" if entry.can_undo else "history"),
            "",
            f"Detalle del cambio · {entry.change_name}",
            "",
            f"{entry.when} · {entry.file_count} archivos · {entry.outcome}",
        ]
        lines.append(
            "Punto de regreso disponible."
            if entry.can_undo
            else "Ya no conserva su punto de regreso: esta entrada no se puede deshacer."
        )
        if entry.rollback_status:
            lines.append(f"Estado del respaldo: {entry.rollback_status}")
        detail.update("\n".join(lines))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self._refresh_selection()

    # -- acciones ------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "undo":
            event.stop()
            self._undo_selected()
            return
        if event.button.id == "more":
            event.stop()
            self._more()
            return
        super().on_button_pressed(event)

    def _more(self) -> None:
        entry = self._selected()
        options: list[tuple[str, str, str]] = []
        if entry is not None:
            options.append(
                (
                    "forget",
                    "Quitar del registro",
                    "Elimina esta entrada de la lista. Tu escritorio no cambia.",
                )
            )
        options.append(
            (
                "clear",
                "Vaciar historial",
                "Quita las entradas que ya no se pueden deshacer.",
            )
        )

        def _chosen(action: str | None) -> None:
            if action == "forget" and entry is not None:
                self._forget(entry)
            elif action == "clear":
                self._clear_all()

        self.app.push_screen(MoreActionsModal("Más acciones", options), _chosen)

    def _undo_selected(self) -> None:
        entry = self._selected()
        if entry is None or not entry.can_undo:
            return

        def _confirmed(accepted: bool | None) -> None:
            if not accepted:
                return
            try:
                result = self.app.activity.undo(entry.transaction_id)
            except UserError as exc:
                self.show_error(exc)
                return
            self._reload()
            self.app.notify(result.message, timeout=6)

        self.app.push_screen(
            ConfirmModal(
                "Volver al estado anterior",
                f"Styler restaurará las rutas que cambió «{entry.change_name}».\n"
                "No tocará tus documentos ni tus programas.",
                "Deshacer el cambio",
            ),
            _confirmed,
        )

    def _forget(self, entry: HistoryEntry) -> None:
        if entry.can_undo:
            title = "Olvidar sin poder deshacer"
            body = (
                f"«{entry.change_name}» todavía se puede deshacer.\n\n"
                "Si la quitas del registro, Styler ya no podrá devolver tu escritorio "
                "al estado anterior. Lo aplicado se queda como está."
            )
            confirm_label = "Quitarla de todos modos"
        else:
            title = "Quitar del registro"
            body = (
                f"«{entry.change_name}» desaparecerá de esta lista.\n\n"
                "Tu escritorio no cambia: esta entrada ya no era reversible."
            )
            confirm_label = "Quitarla"

        def _confirmed(accepted: bool | None) -> None:
            if not accepted:
                return
            try:
                self.app.activity.forget(entry.transaction_id, force=entry.can_undo)
            except UserError as exc:
                self.show_error(exc)
                return
            self._reload()
            self.app.notify("Entrada quitada del registro.", timeout=5)

        self.app.push_screen(ConfirmModal(title, body, confirm_label, danger=True), _confirmed)

    def _clear_all(self) -> None:
        if not self.entries:
            self.app.notify("El historial ya está vacío.", timeout=4)
            return
        reversibles = sum(1 for entry in self.entries.values() if entry.can_undo)

        def _confirmed(accepted: bool | None) -> None:
            if not accepted:
                return
            try:
                removed = self.app.activity.clear_history(include_undoable=False)
            except UserError as exc:
                self.show_error(exc)
                return
            self._reload()
            self.app.notify(
                f"Se quitaron {removed} entradas. "
                + (
                    f"Se conservaron {reversibles} que aún se pueden deshacer."
                    if reversibles
                    else "El historial quedó vacío."
                ),
                timeout=7,
            )

        self.app.push_screen(
            ConfirmModal(
                "Vaciar el historial",
                "Styler quitará las entradas que ya no se pueden deshacer.\n\n"
                + (
                    f"Las {reversibles} que todavía son reversibles se conservan: "
                    "vaciar el historial no debe quitarte la única forma de volver atrás."
                    if reversibles
                    else "Tu escritorio no cambia."
                ),
                "Vaciar",
                danger=True,
            ),
            _confirmed,
        )

class StylerApp(App):
    CSS = _load_tui_css()
    TITLE = "Styler"
    SUB_TITLE = "Cambios reproducibles para Linux"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("ctrl+1", "go('changes')", "Cambios", show=False),
        Binding("ctrl+2", "go('history')", "Actividad", show=False),
        Binding("ctrl+3", "go('tools')", "Herramientas", show=False),
        Binding("ctrl+q", "quit", "Salir"),
        Binding("f1", "help", "Ayuda"),
    ]

    def __init__(
        self,
        root: str | None = None,
        home: str | None = None,
        demo: bool = False,
        open_path: str = "",
        unicode_symbols: bool = True,
    ) -> None:
        super().__init__()
        root_path = ensure_library_root(root)
        self.root = str(root_path)
        root = self.root
        self.demo = demo
        self.open_path = open_path
        self.unicode_symbols = unicode_symbols
        self.writing = False
        self._cancel_modal_open = False

        self.activity = ActivityService(root=root)
        self.baselines = BaselineService(root=root, home=home)
        self.constructor = ChangeConstructorService(root=root, home=home)
        self.portable_library = PortableLibrary(root=root)
        self.changes = ChangeService(root=root, home=home)

    ROUTES = {
        "changes": ChangesScreen,
        "home": ChangesScreen,
        "history": HistoryScreen,
        "tools": ChangeConstructorScreen,
    }

    def on_mount(self) -> None:
        self.push_screen(ChangesScreen())
        if self.open_path:
            try:
                inspection = inspect_package(self.open_path)
                if inspection.manifest.package_type is PackageType.BASELINE:
                    self.baselines.import_package(self.open_path, activate_after=False)
                    self.go("tools")
                else:
                    self.portable_library.import_package(
                        self.open_path, collision_policy="replace_explicitly"
                    )
                    self.go("changes")
            except Exception as exc:
                self.push_screen(ErrorModal(to_user_error(exc)))

    def go(self, route: str) -> None:
        """Cambia de sección; no apila copias.

        Antes cada pulsación de la barra hacía `push_screen()`: la aplicación
        acumulaba pantallas, Esc regresaba a una sección visitada hace rato y
        todo parecía repetirse. Las secciones se reemplazan; los flujos
        secundarios (asistentes, detalles, modales) sí se apilan encima.
        """
        screen_class = self.ROUTES.get(route)
        if screen_class is None:
            return
        if isinstance(self.screen, screen_class):
            return  # ya estamos aquí

        # Si había un flujo secundario abierto, se cierra antes de cambiar.
        while len(self.screen_stack) > 2 and not isinstance(self.screen, tuple(self.ROUTES.values())):
            self.pop_screen()

        if len(self.screen_stack) > 1 and isinstance(self.screen, tuple(self.ROUTES.values())):
            self.switch_screen(screen_class())
        else:
            self.push_screen(screen_class())

    def action_go(self, route: str) -> None:
        self.go(route)

    def action_back(self) -> None:
        """Esc: sale del flujo secundario; desde una sección regresa a Inicio."""
        if not isinstance(self.screen, tuple(self.ROUTES.values())):
            if len(self.screen_stack) > 2:
                self.pop_screen()
            return
        if isinstance(self.screen, ChangesScreen):
            return
        self.go("changes")

    def action_quit(self) -> None:
        if not self.writing:
            self.exit()
            return
        if any(isinstance(screen, ChangeProgressScreen) for screen in self.screen_stack):
            self.notify(
                "Hay una integración en curso. Styler no cerrará mientras el gestor de paquetes esté activo.",
                severity="warning",
                timeout=5,
            )
            return
        self.exit()

    def action_help(self) -> None:
        screen = self.screen
        if isinstance(screen, StylerScreen):
            screen.open_help()
