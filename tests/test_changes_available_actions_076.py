"""0.7.6: un cambio portable se integra y se elimina desde Cambios, sin ocultarlo."""
from __future__ import annotations

from pathlib import Path

import pytest

from styler.changes import ChangeService, ChangeStatus
from styler.portable import PortableLibrary
from tests.test_imported_packages_as_changes_074 import _portable_change


def test_old_portable_package_does_not_need_to_be_recreated_to_build_plan(tmp_path):
    root = tmp_path / "library"
    package_path, change_id, graph = _portable_change(tmp_path, package_id="stacer-old")
    PortableLibrary(root).import_package(package_path)

    service = ChangeService(root=root, home=tmp_path / "home")
    plan = service.build_plan(change_id)

    assert plan.change_id == change_id
    assert plan.workflow == graph.workflow
    assert plan.provider_id == "stylerpkg"


def test_available_portable_change_can_be_deleted_from_its_source(tmp_path):
    root = tmp_path / "library"
    package_path, change_id, _graph = _portable_change(tmp_path)
    PortableLibrary(root).import_package(package_path)
    service = ChangeService(root=root, home=tmp_path / "home")

    assert service.can_delete_available(change_id) is True
    service.delete_available_change(change_id)
    assert change_id not in {item.change_id for item in service.available_changes()}
    assert PortableLibrary(root).list_packages() == ()


def test_builtin_change_is_not_misrepresented_as_a_deletable_local_source(tmp_path):
    service = ChangeService(root=tmp_path / "library", home=tmp_path / "home")
    assert service.can_delete_available("photogimp") is False
    with pytest.raises(ValueError, match="incorporado con Styler"):
        service.delete_available_change("photogimp")


def test_deleting_package_after_apply_keeps_receipts_for_real_removal(tmp_path):
    root = tmp_path / "library"
    home = tmp_path / "home"
    home.mkdir()
    package_path, change_id, _graph = _portable_change(tmp_path)
    PortableLibrary(root).import_package(package_path)
    service = ChangeService(root=root, home=home)

    applied = service.execute(change_id)
    assert applied.ok is True
    assert applied.status == ChangeStatus.INTEGRATED
    target = home / ".config/styler-demo/demo.txt"
    assert target.exists()

    service.delete_available_change(change_id)
    assert change_id not in {item.change_id for item in service.available_changes()}
    assert change_id in {item.change_id for item in service.integrated_changes()}
    assert service.can_rollback(change_id) is True

    removed = service.rollback_change(change_id)
    assert removed.ok is True
    assert not target.exists()


def test_changes_screen_does_not_tie_integrate_to_configure_for_stylerpkg():
    source = Path("styler/tui/app.py").read_text(encoding="utf-8")
    start = source.index("class ChangesScreen(StylerScreen):")
    end = source.index("class ChangeReviewScreen(StylerScreen):")
    block = source[start:end]

    assert 'integrable = available' in block
    assert 'integrate.disabled = not integrable' in block
    assert 'integrate.set_class(not integrable, "hidden")' in block
    assert 'configurable = available and card.provider_id not in {"stylerpkg", "yaml"}' in block
    assert 'delete-available-change' in block


def test_public_package_library_has_no_hide_toggle():
    assert not hasattr(PortableLibrary, "set_enabled")
