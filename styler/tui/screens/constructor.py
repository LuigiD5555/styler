"""Pantalla de herramientas y constructor de cambios."""
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
