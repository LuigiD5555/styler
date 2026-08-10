"""Modelos pequeños compartidos por Cambios y Actividad."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DependencyImpactItemView:
    component_id: str
    title: str
    component_type: str
    affected_titles: tuple[str, ...] = ()
    independent_count: int = 0
    missing_capabilities: tuple[str, ...] = ()
    catalog_note: str = ""

    @property
    def has_dependents(self) -> bool:
        return bool(self.affected_titles)


@dataclass(frozen=True)
class UndoResult:
    ok: bool
    message: str
    technical_detail: str = ""


@dataclass(frozen=True)
class HistoryEntry:
    transaction_id: str
    when: str
    change_name: str
    outcome: str
    file_count: int
    rollback_status: str
    can_undo: bool
