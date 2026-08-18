"""Aplicación Textual y router principal de Styler."""
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


from styler.tui.common import ErrorModal, StylerScreen, _load_tui_css
from styler.tui.screens import (
    ChangesScreen, ChangeProgressScreen, ChangeConstructorScreen, HistoryScreen,
    ChangeReviewScreen, ChangeResultScreen, ChangeBatchReviewScreen,
    ChangeBatchProgressScreen, ChangeBatchResultScreen,
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
