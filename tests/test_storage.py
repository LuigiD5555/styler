from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from styler.models import Decision, FileEntry, State
from styler.objectstore import ObjectStore, ObjectStoreError
from styler.runtime.models import Status
from styler.snapshot import (
    Snapshot,
    create_snapshot,
    list_snapshots,
    load_snapshot,
    materialize_files,
    restore_snapshot,
    save_snapshot,
)
from styler.transaction import apply_snapshot_transactional, list_transactions, load_transaction


# ---------------------------------------------------------------------------
# ObjectStore
# ---------------------------------------------------------------------------

def test_object_store_stores_and_restores_content():
    with TemporaryDirectory() as temp:
        source = Path(temp) / "source.txt"
        source.write_text("contenido de prueba")
        store = ObjectStore(root=temp)

        checksum, object_path = store.store_file(source)
        assert store.has(checksum)
        assert Path(object_path).is_file()

        destination = Path(temp) / "restored" / "file.txt"
        store.restore_file(checksum, destination)
        assert destination.read_text() == "contenido de prueba"


def test_object_store_deduplicates_identical_content():
    with TemporaryDirectory() as temp:
        a = Path(temp) / "a.txt"
        b = Path(temp) / "b.txt"
        a.write_text("mismo contenido")
        b.write_text("mismo contenido")
        store = ObjectStore(root=temp)

        checksum_a, path_a = store.store_file(a)
        checksum_b, path_b = store.store_file(b)

        assert checksum_a == checksum_b
        assert path_a == path_b
        assert store.object_count() == 1


def test_object_store_rejects_missing_object():
    with TemporaryDirectory() as temp:
        store = ObjectStore(root=temp)
        try:
            store.restore_file("0" * 32, Path(temp) / "out.txt")
            assert False, "debía fallar con un checksum inexistente"
        except ObjectStoreError:
            pass


def test_object_store_verify_detects_corruption():
    with TemporaryDirectory() as temp:
        source = Path(temp) / "source.txt"
        source.write_text("original")
        store = ObjectStore(root=temp)
        checksum, object_path = store.store_file(source)
        assert store.verify(checksum)

        Path(object_path).write_text("corrompido")
        assert not store.verify(checksum)


# ---------------------------------------------------------------------------
# Snapshots restaurables
# ---------------------------------------------------------------------------

def _make_home_file(root: str, relative: str, content: str) -> FileEntry:
    """Crea un archivo real en un HOME de prueba y devuelve la FileEntry
    portable (${HOME}/...) que un observer produciría."""
    home = Path(root) / "home"
    real = home / relative
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text(content)
    return FileEntry(path=f"${{HOME}}/{relative}", checksum="pending", size=len(content))


def test_materialize_files_fills_object_path_for_existing_files():
    with TemporaryDirectory() as temp:
        home = Path(temp) / "home"
        home.mkdir()
        target = home / ".config" / "app.conf"
        target.parent.mkdir(parents=True)
        target.write_text("tema=oscuro")

        import styler.snapshot as snapshot_mod
        original_home = Path.home
        snapshot_mod.Path.home = staticmethod(lambda: home)  # type: ignore[assignment]
        try:
            entry = FileEntry(path="${HOME}/.config/app.conf", checksum="", size=0)
            store = ObjectStore(root=temp)
            materialized = materialize_files([entry], store)
        finally:
            snapshot_mod.Path.home = original_home  # type: ignore[assignment]

        assert len(materialized) == 1
        assert materialized[0].object_path
        assert store.verify(materialized[0].checksum)


def test_materialize_files_leaves_object_path_empty_for_missing_file():
    with TemporaryDirectory() as temp:
        store = ObjectStore(root=temp)
        entry = FileEntry(path="${HOME}/.config/no-existe.conf", checksum="deadbeef", size=0)
        materialized = materialize_files([entry], store)
        assert materialized[0].object_path == ""


def test_snapshot_save_load_and_list_roundtrip():
    with TemporaryDirectory() as temp:
        state = State(state_id="abc123", label="prueba")
        snapshot = Snapshot(snapshot_id="abc123", label="prueba", state=state)
        save_snapshot(snapshot, root=temp)

        assert list_snapshots(root=temp) == ["abc123"]
        loaded = load_snapshot("abc123", root=temp)
        assert loaded.label == "prueba"
        assert loaded.state.state_id == "abc123"


def test_restore_snapshot_dry_run_does_not_touch_disk():
    with TemporaryDirectory() as temp:
        home = Path(temp) / "home"
        target_file = home / ".config" / "theme.conf"
        target_file.parent.mkdir(parents=True)
        target_file.write_text("original")

        store = ObjectStore(root=temp)
        checksum, object_path = store.store_file(_write_and_get(home, "restored.conf", "nuevo"))

        state = State(
            state_id="snap1",
            label="Escritorio creativo",
            files=[
                FileEntry(
                    path="${HOME}/.config/theme.conf",
                    checksum=checksum,
                    size=5,
                    object_path=object_path,
                )
            ],
        )
        snapshot = Snapshot(snapshot_id="snap1", label="Escritorio creativo", state=state)
        save_snapshot(snapshot, root=temp)

        run = restore_snapshot("snap1", root=temp, execute=False, approve=True)
        assert run.dry_run is True
        assert target_file.read_text() == "original"  # no se tocó nada


def _write_and_get(home: Path, relative: str, content: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / relative
    path.write_text(content)
    return path


def test_restore_snapshot_execute_applies_real_content():
    with TemporaryDirectory() as temp:
        home = Path(temp) / "home"
        home.mkdir()
        source_content = _write_and_get(home, "source-material.conf", "tema=oscuro-nuevo")

        store = ObjectStore(root=temp)
        checksum, object_path = store.store_file(source_content)

        import styler.runtime.executors as executors_mod
        original_home = Path.home
        executors_mod.Path.home = staticmethod(lambda: home)  # type: ignore[assignment]
        try:
            state = State(
                state_id="snap2",
                label="Tema oscuro",
                files=[
                    FileEntry(
                        path="${HOME}/.config/theme.conf",
                        checksum=checksum,
                        size=source_content.stat().st_size,
                        object_path=object_path,
                    )
                ],
            )
            snapshot = Snapshot(snapshot_id="snap2", label="Tema oscuro", state=state)
            save_snapshot(snapshot, root=temp)

            run = restore_snapshot("snap2", root=temp, execute=True, approve=True)
            assert run.success, [r.message for r in run.results]

            restored = home / ".config" / "theme.conf"
            assert restored.read_text() == "tema=oscuro-nuevo"
        finally:
            executors_mod.Path.home = original_home  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Transacciones y rollback
# ---------------------------------------------------------------------------

def test_transactional_apply_rolls_back_on_partial_failure():
    with TemporaryDirectory() as temp:
        home = Path(temp) / "home"
        home.mkdir()
        existing = home / ".config" / "theme.conf"
        existing.parent.mkdir(parents=True)
        existing.write_text("estado-actual")

        store = ObjectStore(root=temp)
        first_source = Path(temp) / "first"
        first_source.write_text("estado-nuevo")
        first_checksum, _ = store.store_file(first_source)
        second_source = Path(temp) / "second"
        second_source.write_text("archivo-nuevo")
        second_checksum, _ = store.store_file(second_source)

        state = State(
            state_id="partial-failure",
            label="Aplicación parcial",
            files=[
                FileEntry(path="${HOME}/.config/theme.conf", checksum=first_checksum),
                FileEntry(path="${HOME}/.config/new.conf", checksum=second_checksum),
            ],
        )
        save_snapshot(Snapshot("partial-failure", "Aplicación parcial", state=state), root=temp)

        run, record = apply_snapshot_transactional(
            "partial-failure",
            root=temp,
            execute=True,
            approve=True,
            home=home,
            _fault_after=1,
        )

        assert run.success is False
        assert record.backup_snapshot
        assert record.rolled_back is True
        assert record.rollback_status == "completed"
        assert existing.read_text() == "estado-actual"
        assert not (home / ".config" / "new.conf").exists()
        assert list_transactions(root=temp) == [record.transaction_id]
        assert load_transaction(record.transaction_id, root=temp).journal_path


def test_transactional_apply_preview_creates_no_backup():
    with TemporaryDirectory() as temp:
        state = State(state_id="preview1", label="Vista previa", files=[])
        snapshot = Snapshot(snapshot_id="preview1", label="Vista previa", state=state)
        save_snapshot(snapshot, root=temp)

        run, record = apply_snapshot_transactional("preview1", root=temp, execute=False)
        assert run.dry_run is True
        assert record.backup_snapshot == ""
        assert list_snapshots(root=temp) == ["preview1"]


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"OK   {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} pruebas pasaron")
    raise SystemExit(1 if failures else 0)
