"""Pantallas del flujo de integración de cambios."""
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
                ProcessRunner().spawn_detached(["xdg-open", folder])
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
