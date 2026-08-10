"""Índice liviano para inventarios grandes mostrados por la TUI.

El inventario completo permanece como datos. La interfaz sólo recibe una página
acotada y nunca necesita montar un widget por cada paquete del sistema.
"""
from __future__ import annotations

from dataclasses import dataclass

from styler.ui.provenance import ApplicationView, InventoryView


def _search_key(application: ApplicationView) -> str:
    return " ".join(
        part
        for part in (
            application.name,
            application.version,
            application.origin_label,
            application.origin_detail,
            application.upstream,
            application.install_reason,
            application.manager,
        )
        if part
    ).casefold()


@dataclass(frozen=True)
class IndexedApplication:
    application: ApplicationView
    widget_id: str
    search_key: str


@dataclass(frozen=True)
class InventoryPage:
    items: tuple[ApplicationView, ...]
    total_matches: int
    page: int
    page_size: int

    @property
    def page_count(self) -> int:
        if self.total_matches == 0:
            return 0
        return (self.total_matches + self.page_size - 1) // self.page_size

    @property
    def first_number(self) -> int:
        return 0 if not self.items else self.page * self.page_size + 1

    @property
    def last_number(self) -> int:
        return self.page * self.page_size + len(self.items)


class InventoryIndex:
    """Índice inmutable con búsquedas simples y paginación estable."""

    def __init__(self, inventory: InventoryView, *, safe_id) -> None:
        indexed = tuple(
            IndexedApplication(
                application=application,
                widget_id=safe_id(application.app_id),
                search_key=_search_key(application),
            )
            for application in inventory.applications
        )
        self._all = indexed
        self._at_risk = tuple(item for item in indexed if not item.application.recoverable)
        self.by_widget_id = {item.widget_id: item.application for item in indexed}
        self.by_app_id = {item.application.app_id: item.application for item in indexed}
        self.at_risk_count = len(self._at_risk)

    def page(
        self,
        *,
        query: str = "",
        only_at_risk: bool = False,
        page: int = 0,
        page_size: int = 100,
    ) -> InventoryPage:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        source = self._at_risk if only_at_risk else self._all
        needle = query.strip().casefold()
        requested_page = max(page, 0)
        requested_start = requested_page * page_size
        total = 0
        selected: list[ApplicationView] = []
        for item in source:
            if needle and needle not in item.search_key:
                continue
            if requested_start <= total < requested_start + page_size:
                selected.append(item.application)
            total += 1

        max_page = max(0, (total - 1) // page_size) if total else 0
        normalized_page = min(requested_page, max_page)
        if normalized_page != requested_page:
            start = normalized_page * page_size
            selected = []
            match_index = 0
            for item in source:
                if needle and needle not in item.search_key:
                    continue
                if start <= match_index < start + page_size:
                    selected.append(item.application)
                match_index += 1

        return InventoryPage(
            items=tuple(selected),
            total_matches=total,
            page=normalized_page,
            page_size=page_size,
        )
