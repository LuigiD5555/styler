from __future__ import annotations

from dataclasses import dataclass

from styler.ui.selection_index import SelectionIndex


@dataclass(frozen=True)
class Item:
    item_id: str
    title: str
    description: str = ""


@dataclass(frozen=True)
class Category:
    title: str
    items: tuple[Item, ...]


def test_selection_index_bounds_large_collections() -> None:
    categories = (
        Category("Aplicaciones", tuple(Item(f"item-{i}", f"Aplicación {i}") for i in range(20_000))),
    )
    index = SelectionIndex(categories)

    first = index.page(page_size=100)
    last = index.page(page=199, page_size=100)

    assert index.total == 20_000
    assert len(first.entries) == 100
    assert len(last.entries) == 100
    assert first.page_count == 200


def test_selection_index_precomputes_search_and_finds_photogimp() -> None:
    categories = (
        Category("Aplicaciones", tuple(Item(f"item-{i}", f"Aplicación {i}") for i in range(5_000))),
        Category("Otros", (Item("photogimp", "PhotoGIMP", "Configuración visual de GIMP"),)),
    )
    index = SelectionIndex(categories)

    page = index.page(query="photogimp")

    assert page.total_matches == 1
    assert page.entries[0].item.item_id == "photogimp"
    assert index.by_item_id["photogimp"].title == "PhotoGIMP"


def test_selection_index_searches_category_and_description() -> None:
    categories = (
        Category("Apariencia", (Item("theme", "Tema oscuro", "Colores del escritorio"),)),
    )
    index = SelectionIndex(categories)

    assert index.page(query="apariencia").total_matches == 1
    assert index.page(query="escritorio").total_matches == 1
