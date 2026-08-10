"""Widgets exclusivos del Constructor de cambios (pestaña Herramientas)."""
from __future__ import annotations

from uuid import uuid4

from textual.app import ComposeResult
from textual.widgets import ListItem, Static

from styler.tui.icons import icon


def unique_id(prefix: str) -> str:
    """Identificador irrepetible para listas reconstruidas de forma asíncrona."""
    return f"{prefix}-{uuid4().hex[:10]}"


class ConstructorStatic(Static):
    """Texto de fila sin selección arbitraria del ratón.

    Textual intenta iniciar una selección de texto al hacer clic sobre un
    ``Static``. Dentro de ``ListView`` algunas versiones no encuentran el
    contenedor desplazable y terminan accediendo a ``None.region``. Estas
    etiquetas son controles de lista, no documentos seleccionables.
    """

    ALLOW_SELECT = False


class ConstructorRow(ListItem):
    """Base visual: nombre, metadatos y estado con borde semántico."""

    ALLOW_SELECT = False

    def __init__(self, *, prefix: str, name: str, meta: str, state: str, glyph: str = "") -> None:
        super().__init__(id=unique_id(prefix), classes=f"constructor-row {state}")
        self.row_name = name
        self.row_meta = meta
        self.row_state = state
        self.row_glyph = glyph

    def compose(self) -> ComposeResult:
        prefix = f"{self.row_glyph}  " if self.row_glyph else ""
        yield ConstructorStatic(f"{prefix}{self.row_name}", classes="row-name", markup=False)
        yield ConstructorStatic(self.row_meta, classes="row-meta", markup=False)


class DetectedRow(ConstructorRow):
    """Cambio detectado: listo, elegido o pendiente de revisión."""

    def __init__(self, item, *, chosen: bool = False) -> None:
        if chosen:
            state, glyph = "chosen", icon("selection") or "[x]"
        elif item.exportable:
            state, glyph = "ready", icon("success") or "+"
        else:
            state, glyph = "review", icon("warning") or "!"
        super().__init__(
            prefix="detected",
            name=item.name,
            meta=item.status_line,
            state=state,
            glyph=glyph,
        )
        self.change_id = item.change_id
        self.exportable = item.exportable
        self.reason = item.reason


class BaselineRow(ConstructorRow):
    """Línea base; la activa usa el estado elegido."""

    def __init__(self, item, *, active: bool) -> None:
        labels = ", ".join(item.labels) or item.kind_label
        super().__init__(
            prefix="baseline",
            name=item.name,
            meta=f"{item.system_label} · {labels}",
            state="chosen" if active else ("review" if item.damaged else "ready"),
            glyph=(icon("success") if active else icon("baseline")) or ("*" if active else "-"),
        )
        self.baseline_id = item.baseline_id


class SavedPackageRow(ConstructorRow):
    """Paquete guardado; todo paquete de cambio registrado aparece en Cambios."""

    def __init__(self, package) -> None:
        super().__init__(
            prefix="package",
            name=package.manifest.name,
            meta=f"{package.identity} · registrado en Styler",
            state="ready",
            glyph=icon("save") or "#",
        )
        self.package_id = package.manifest.package_id
        self.package_version = package.manifest.version
