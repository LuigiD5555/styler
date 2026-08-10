from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from styler.changes import AutomationLevel, ChangeCard, ChangeStatus
from styler.tui.app import ChangesScreen


class FakeListView:
    def __init__(self) -> None:
        self.children: list[object] = []
        self.index: int | None = None

    async def clear(self):
        # Reproduce el contrato de Textual: clear no termina hasta recibir await.
        await asyncio.sleep(0)
        self.children.clear()

    async def extend(self, items):
        for item in items:
            await self.append(item)

    async def append(self, item):
        await asyncio.sleep(0)
        item_id = getattr(item, "id", None)
        if item_id and any(getattr(existing, "id", None) == item_id for existing in self.children):
            raise RuntimeError(f"duplicate id: {item_id}")
        self.children.append(item)


class FakeChangeService:
    @staticmethod
    def available_changes():
        return (
            ChangeCard(
                change_id="photogimp",
                name="PhotoGIMP",
                description="test",
                category="test",
                status=ChangeStatus.AVAILABLE,
                status_label="Disponible",
                provider_id="flatpak",
                provider_label="Flathub",
                automation_level=AutomationLevel.AUTOMATIC,
            ),
        )

    @staticmethod
    def integrated_changes():
        return ()


class FakeScreen:
    def __init__(self) -> None:
        self._refresh_lock = asyncio.Lock()
        self.available = FakeListView()
        self.integrated = FakeListView()
        self.app = SimpleNamespace(changes=FakeChangeService())

    def query_one(self, selector, _widget_type):
        return self.available if selector == "#available-changes" else self.integrated


def test_changes_refresh_waits_for_removal_before_mounting_same_ids_again():
    screen = FakeScreen()

    async def scenario():
        await ChangesScreen.refresh_changes(screen)
        await ChangesScreen.refresh_changes(screen)

    asyncio.run(scenario())

    assert [item.id for item in screen.available.children] == ["available-change-photogimp"]
    assert [item.id for item in screen.integrated.children] == ["integrated-clean-state"]
