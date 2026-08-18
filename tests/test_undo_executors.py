"""Los ejecutores de reversión: conservadores, idempotentes y confinados al HOME."""
from __future__ import annotations

from styler.planning.models import ExecutionContext, Status, StepDefinition
from styler.execution.undo import (
    RemovePathsExecutor,
    RestoreBackupExecutor,
    UndoNoteExecutor,
)


def _ctx(tmp_path, home, *, dry_run=False):
    return ExecutionContext(root=tmp_path, dry_run=dry_run, values={"home": str(home)})


def _home(tmp_path):
    home = tmp_path / "home" / "luis"
    home.mkdir(parents=True)
    return home


def test_restore_backup_returns_previous_content(tmp_path):
    home = _home(tmp_path)
    config = home / ".config" / "GIMP"
    config.mkdir(parents=True)
    (config / "sessionrc").write_text("modificado por photogimp")
    backup = tmp_path / "backup" / "GIMP"
    backup.mkdir(parents=True)
    (backup / "sessionrc").write_text("original")

    step = StepDefinition(
        id="undo.0", step_type="undo_restore_backup",
        config={"source": str(config), "backup": str(backup), "existed": True},
    )
    result = RestoreBackupExecutor().run(step, _ctx(tmp_path, home))
    assert result.success
    assert result.status == Status.ROLLED_BACK
    assert (config / "sessionrc").read_text() == "original"


def test_when_nothing_existed_before_the_undo_removes_what_was_created(tmp_path):
    home = _home(tmp_path)
    config = home / ".config" / "GIMP"
    config.mkdir(parents=True)
    step = StepDefinition(
        id="undo.0", step_type="undo_restore_backup",
        config={"source": str(config), "backup": "", "existed": False},
    )
    result = RestoreBackupExecutor().run(step, _ctx(tmp_path, home))
    assert result.success
    assert not config.exists()


def test_missing_backup_fails_loudly_instead_of_deleting(tmp_path):
    home = _home(tmp_path)
    config = home / ".config" / "GIMP"
    config.mkdir(parents=True)
    (config / "sessionrc").write_text("algo")
    step = StepDefinition(
        id="undo.0", step_type="undo_restore_backup",
        config={"source": str(config), "backup": str(tmp_path / "no-existe"), "existed": True},
    )
    result = RestoreBackupExecutor().run(step, _ctx(tmp_path, home))
    assert not result.success
    assert result.data["error_code"] == "UNDO_BACKUP_MISSING"
    # No empeora el estado: lo que había sigue ahí.
    assert (config / "sessionrc").exists()


def test_paths_outside_home_are_never_touched(tmp_path):
    home = _home(tmp_path)
    outsider = tmp_path / "etc" / "importante.conf"
    outsider.parent.mkdir(parents=True)
    outsider.write_text("no me borres")
    step = StepDefinition(
        id="undo.0", step_type="undo_remove_paths", config={"created_paths": [str(outsider)]}
    )
    result = RemovePathsExecutor().run(step, _ctx(tmp_path, home))
    assert outsider.exists()
    assert result.data["skipped_outside_home"] == [str(outsider)]


def test_removing_the_home_itself_is_refused(tmp_path):
    home = _home(tmp_path)
    step = StepDefinition(
        id="undo.0", step_type="undo_remove_paths", config={"created_paths": [str(home)]}
    )
    RemovePathsExecutor().run(step, _ctx(tmp_path, home))
    assert home.exists()


def test_undo_is_idempotent(tmp_path):
    home = _home(tmp_path)
    target = home / ".local" / "share" / "gimp.desktop"
    target.parent.mkdir(parents=True)
    target.write_text("x")
    step = StepDefinition(
        id="undo.0", step_type="undo_remove_paths", config={"created_paths": [str(target)]}
    )
    first = RemovePathsExecutor().run(step, _ctx(tmp_path, home))
    second = RemovePathsExecutor().run(step, _ctx(tmp_path, home))
    assert first.success and second.success
    assert second.data["missing"] == [str(target)]


def test_deeper_paths_are_removed_first(tmp_path):
    home = _home(tmp_path)
    folder = home / ".config" / "app"
    folder.mkdir(parents=True)
    inner = folder / "inner.conf"
    inner.write_text("x")
    step = StepDefinition(
        id="undo.0", step_type="undo_remove_paths",
        config={
            "created_paths": [str(inner)],
            "created_directories": [str(folder)],
        },
    )
    result = RemovePathsExecutor().run(step, _ctx(tmp_path, home))
    assert result.success
    assert not folder.exists()


def test_dry_run_changes_nothing(tmp_path):
    home = _home(tmp_path)
    target = home / ".config" / "x.conf"
    target.parent.mkdir(parents=True)
    target.write_text("x")
    step = StepDefinition(
        id="undo.0", step_type="undo_remove_paths", config={"created_paths": [str(target)]}
    )
    result = RemovePathsExecutor().run(step, _ctx(tmp_path, home, dry_run=True))
    assert result.status == Status.DRY_RUN
    assert target.exists()


def test_undo_note_is_informative_and_never_fails(tmp_path):
    home = _home(tmp_path)
    step = StepDefinition(
        id="undo.0", step_type="undo_note", description="GIMP se instaló durante el cambio."
    )
    result = UndoNoteExecutor().run(step, _ctx(tmp_path, home))
    assert result.success
    assert result.status == Status.WAITING_FOR_USER
    assert result.data["fully_reverted"] is False


def test_preexisting_directory_is_never_removed_recursively(tmp_path):
    home = _home(tmp_path)
    folder = home / ".config" / "app"
    folder.mkdir(parents=True)
    original = folder / "user.conf"
    original.write_text("usuario")
    created = folder / "styler.conf"
    created.write_text("styler")
    step = StepDefinition(
        id="undo.0", step_type="undo_remove_paths",
        config={
            "created_paths": [str(created)],
            "created_directories": [str(folder)],
        },
    )
    result = RemovePathsExecutor().run(step, _ctx(tmp_path, home))
    assert result.success
    assert original.read_text() == "usuario"
    assert not created.exists()
    assert folder.exists()
    assert result.data["fully_reverted"] is False


def test_overwritten_file_is_restored_from_its_individual_backup(tmp_path):
    home = _home(tmp_path)
    target = home / ".config" / "app.conf"
    target.parent.mkdir(parents=True)
    target.write_text("nuevo")
    backup = tmp_path / "write-backup" / "app.conf"
    backup.parent.mkdir(parents=True)
    backup.write_text("original")
    step = StepDefinition(
        id="undo.0", step_type="undo_remove_paths",
        config={"overwritten": [{"path": str(target), "backup": str(backup)}]},
    )
    result = RemovePathsExecutor().run(step, _ctx(tmp_path, home))
    assert result.success
    assert result.status == Status.ROLLED_BACK
    assert target.read_text() == "original"
    assert result.data["fully_reverted"] is True


def test_full_directory_restore_uses_swap_without_leaving_temporary_trees(tmp_path):
    home = _home(tmp_path)
    config = home / ".config" / "GIMP"
    config.mkdir(parents=True)
    (config / "current").write_text("nuevo")
    backup = tmp_path / "backup" / "GIMP"
    backup.mkdir(parents=True)
    (backup / "sessionrc").write_text("original")
    step = StepDefinition(
        id="undo.swap", step_type="undo_restore_backup",
        config={"source": str(config), "backup": str(backup), "existed": True},
    )
    result = RestoreBackupExecutor().run(step, _ctx(tmp_path, home))
    assert result.success
    assert (config / "sessionrc").read_text() == "original"
    assert not (config / "current").exists()
    assert not list(config.parent.glob(".GIMP.styler-*"))


def test_individual_file_restore_replaces_symlink_instead_of_following_it(tmp_path):
    home = _home(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("no tocar")
    target = home / ".config" / "app.conf"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)
    backup = tmp_path / "backup" / "app.conf"
    backup.parent.mkdir(parents=True)
    backup.write_text("original")
    step = StepDefinition(
        id="undo.file", step_type="undo_remove_paths",
        config={"overwritten": [{"path": str(target), "backup": str(backup)}]},
    )
    result = RemovePathsExecutor().run(step, _ctx(tmp_path, home))
    assert result.success
    assert target.is_file() and not target.is_symlink()
    assert target.read_text() == "original"
    assert outside.read_text() == "no tocar"
