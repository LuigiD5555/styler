"""Índice paginado para la pantalla de selección de captura.

El modelo completo permanece como datos; la TUI recibe sólo una página acotada.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class IndexedSelectionItem:
    category_index: int
    category_title: str
    item: Any
    search_key: str


@dataclass(frozen=True)
class SelectionPage:
    entries: tuple[IndexedSelectionItem, ...]
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
        return 0 if not self.entries else self.page * self.page_size + 1

    @property
    def last_number(self) -> int:
        return self.page * self.page_size + len(self.entries)


class SelectionIndex:
    """Índice inmutable para filtrar sin reconstruir cadenas en cada tecla."""

    def __init__(self, categories: Iterable[Any]) -> None:
        entries: list[IndexedSelectionItem] = []
        by_item_id: dict[str, Any] = {}
        for category_index, category in enumerate(categories):
            category_title = str(category.title)
            for item in category.items:
                key = " ".join(
                    part
                    for part in (
                        str(item.title),
                        str(item.description or ""),
                        category_title,
                    )
                    if part
                ).casefold()
                entries.append(
                    IndexedSelectionItem(
                        category_index=category_index,
                        category_title=category_title,
                        item=item,
                        search_key=key,
                    )
                )
                by_item_id[str(item.item_id)] = item
        self._entries = tuple(entries)
        self.by_item_id = by_item_id
        self.total = len(self._entries)

    def page(self, *, query: str = "", page: int = 0, page_size: int = 100) -> SelectionPage:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        needle = query.strip().casefold()
        matches = self._entries if not needle else tuple(
            entry for entry in self._entries if needle in entry.search_key
        )
        total = len(matches)
        max_page = max(0, (total - 1) // page_size) if total else 0
        normalized_page = min(max(page, 0), max_page)
        start = normalized_page * page_size
        return SelectionPage(
            entries=matches[start : start + page_size],
            total_matches=total,
            page=normalized_page,
            page_size=page_size,
        )
