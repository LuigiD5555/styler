"""El registro de actividad se conserva sin depender de formatos portables paralelos."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from styler import transaction as transaction_mod
from styler.services import UserError
from styler.ui.activity import ActivityService


def _write_transaction(
    root: Path,
    transaction_id: str,
    *,
    applied: bool = True,
    rolled_back: bool = False,
    with_journal: bool = True,
) -> None:
    transactions = root / ".styler" / "transactions"
    journals = root / ".styler" / "journals"
    transactions.mkdir(parents=True, exist_ok=True)
    journals.mkdir(parents=True, exist_ok=True)

    journal_path = ""
    if with_journal:
        journal_path = str(journals / f"{transaction_id}.json")
        (journals / f"{transaction_id}.json").write_text(
            json.dumps({"transaction_id": transaction_id, "entries": []}),
            encoding="utf-8",
        )

    record = transaction_mod.TransactionRecord(
        transaction_id=transaction_id,
        started_at=1.0,
        finished_at=2.0,
        applied=applied,
        rolled_back=rolled_back,
        source_type="change",
        source_id="cambio-x",
        journal_path=journal_path,
    )
    (transactions / f"{transaction_id}.json").write_text(
        json.dumps(record.to_dict(), ensure_ascii=False), encoding="utf-8"
    )


def test_no_se_olvida_por_accidente_algo_que_todavia_se_puede_deshacer(tmp_path):
    _write_transaction(tmp_path, "aaaaaaaa")
    with pytest.raises(transaction_mod.TransactionInUseError):
        transaction_mod.forget_transaction("aaaaaaaa", root=str(tmp_path))
    assert transaction_mod.list_transactions(str(tmp_path)) == ["aaaaaaaa"]


def test_olvidar_con_confirmacion_borra_registro_y_diario(tmp_path):
    _write_transaction(tmp_path, "aaaaaaaa")
    transaction_mod.forget_transaction("aaaaaaaa", root=str(tmp_path), force=True)
    assert transaction_mod.list_transactions(str(tmp_path)) == []
    assert not (tmp_path / ".styler" / "journals" / "aaaaaaaa.json").exists()


def test_olvidar_una_entrada_ya_deshecha_no_necesita_forzarse(tmp_path):
    _write_transaction(tmp_path, "bbbbbbbb", rolled_back=True)
    transaction_mod.forget_transaction("bbbbbbbb", root=str(tmp_path))
    assert transaction_mod.list_transactions(str(tmp_path)) == []


def test_vaciar_el_registro_conserva_lo_reversible(tmp_path):
    _write_transaction(tmp_path, "aaaaaaaa")
    _write_transaction(tmp_path, "bbbbbbbb", rolled_back=True)
    _write_transaction(tmp_path, "cccccccc", with_journal=False)
    forgotten = transaction_mod.purge_transactions(str(tmp_path))
    assert sorted(forgotten) == ["bbbbbbbb", "cccccccc"]
    assert transaction_mod.list_transactions(str(tmp_path)) == ["aaaaaaaa"]


def test_vaciar_todo_es_posible_pero_hay_que_pedirlo(tmp_path):
    _write_transaction(tmp_path, "aaaaaaaa")
    forgotten = transaction_mod.purge_transactions(str(tmp_path), include_undoable=True)
    assert forgotten == ["aaaaaaaa"]
    assert transaction_mod.list_transactions(str(tmp_path)) == []


def test_servicio_traduce_el_bloqueo_a_un_error_de_persona(tmp_path):
    _write_transaction(tmp_path, "aaaaaaaa")
    service = ActivityService(root=str(tmp_path))
    with pytest.raises(UserError) as error:
        service.forget("aaaaaaaa")
    assert "deshacer" in str(error.value).lower()
    service.forget("aaaaaaaa", force=True)
    assert service.history() == []


def test_servicio_vacia_el_registro_y_cuenta_lo_quitado(tmp_path):
    _write_transaction(tmp_path, "aaaaaaaa")
    _write_transaction(tmp_path, "bbbbbbbb", rolled_back=True)
    service = ActivityService(root=str(tmp_path))
    assert service.clear_history() == 1
    remaining = service.history()
    assert [entry.transaction_id for entry in remaining] == ["aaaaaaaa"]
    assert remaining[0].can_undo is True


def test_el_historial_marca_lo_que_se_puede_deshacer(tmp_path):
    _write_transaction(tmp_path, "aaaaaaaa")
    _write_transaction(tmp_path, "bbbbbbbb", rolled_back=True)
    entries = {e.transaction_id: e for e in ActivityService(root=str(tmp_path)).history()}
    assert entries["aaaaaaaa"].can_undo is True
    assert entries["bbbbbbbb"].can_undo is False
