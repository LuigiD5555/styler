"""Actividad reversible de Styler, sin formatos portables paralelos."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from styler import transaction as transaction_mod
from styler.services import UserError
from styler.ui.models import HistoryEntry, UndoResult
from styler.validation import ValidationError


def _journal_size(path: str) -> int:
    if not path:
        return 0
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return 0
    entries = data.get("entries", []) if isinstance(data, dict) else []
    return len(entries) if isinstance(entries, list) else 0


class ActivityService:
    """Lee y revierte operaciones ya registradas por Styler."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = str(root)

    def history(self) -> list[HistoryEntry]:
        entries: list[HistoryEntry] = []
        for identifier in transaction_mod.list_transactions(self.root):
            record = transaction_mod.load_transaction(identifier, self.root)
            entries.append(
                HistoryEntry(
                    transaction_id=record.transaction_id,
                    when=datetime.fromtimestamp(record.started_at).strftime("%d/%m/%Y %H:%M"),
                    change_name=record.source_id or record.transaction_id,
                    outcome=(
                        "Aplicada"
                        if record.applied and not record.rolled_back
                        else "Deshecha"
                        if record.rolled_back
                        else "No completada"
                    ),
                    file_count=_journal_size(record.journal_path),
                    rollback_status=record.rollback_status or "",
                    can_undo=bool(record.applied and record.journal_path and not record.rolled_back),
                )
            )
        return sorted(entries, key=lambda item: item.transaction_id, reverse=True)

    def undo(self, transaction_id: str) -> UndoResult:
        try:
            reverted = transaction_mod.rollback_transaction(transaction_id, self.root)
        except (ValueError, OSError, ValidationError) as exc:
            raise UserError("No se pudo deshacer este cambio.", str(exc)) from exc
        if reverted.rolled_back and reverted.rollback_status == transaction_mod.RollbackStatus.COMPLETED:
            return UndoResult(True, "Styler restauró las rutas a su estado anterior.")
        return UndoResult(
            False,
            "Styler no pudo restaurar todas las rutas. Revisa los detalles técnicos.",
            reverted.error or "",
        )

    def forget(self, transaction_id: str, *, force: bool = False) -> None:
        try:
            transaction_mod.forget_transaction(transaction_id, self.root, force=force)
        except transaction_mod.TransactionInUseError as exc:
            raise UserError(str(exc)) from exc
        except (OSError, ValueError, ValidationError) as exc:
            raise UserError("No se pudo quitar esta entrada del registro.", str(exc)) from exc

    def clear_history(self, *, include_undoable: bool = False) -> int:
        try:
            return len(
                transaction_mod.purge_transactions(
                    self.root, include_undoable=include_undoable
                )
            )
        except (OSError, ValueError, ValidationError) as exc:
            raise UserError("No se pudo vaciar el registro de cambios.", str(exc)) from exc
