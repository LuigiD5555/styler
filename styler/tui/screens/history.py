"""Pantalla de actividad e historial."""
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


from styler.tui.common import (
    ChangeRow, ConfirmModal, ErrorModal, MoreActionsModal,
    ProviderSettingsModal, StylerScreen,
)

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
