"""Contrato de terminar un paquete y abrir un ciclo nuevo sin repetir lo ya empaquetado."""
from __future__ import annotations

from pathlib import Path

from styler.baselines import BaselineKind
from styler.portable import PortableLibrary, inspect_package
from styler.provenance import inventory as inventory_mod
from styler.ui.constructor import ChangeConstructorService

from tests.test_change_constructor_070 import _apt_app, _inventory


def _ready_service(tmp_path: Path) -> ChangeConstructorService:
    root = tmp_path / "library"
    home = tmp_path / "home"
    service = ChangeConstructorService(root=root, home=home)
    service.baselines.register_inventory(
        _inventory("base-cycle"),
        kind=BaselineKind.CUSTOM,
        baseline_id="base-cycle",
        name="Base del ciclo",
        activate_after=True,
    )
    inventory_mod.save_inventory(
        _inventory("current-cycle", applications=[_apt_app("stacer")]),
        root=root,
    )
    return service


def _build_and_register_stacer(service: ChangeConstructorService, tmp_path: Path):
    service.select(["apt:stacer"])
    plan = service.generated_plan("stacer", "Stacer")
    result = service.build_package(tmp_path / "stacer", "stacer", "Stacer", plan=plan)
    PortableLibrary(root=service.root).import_package(
        result.path, collision_policy="replace_explicitly"
    )
    return result


def test_begin_next_cycle_preserves_baseline_but_requires_a_new_scan(tmp_path):
    service = _ready_service(tmp_path)
    _build_and_register_stacer(service, tmp_path)

    summary = service.begin_next_cycle()

    assert summary.has_baseline is True
    assert summary.baseline_id == "base-cycle"
    assert summary.current_id == ""
    assert summary.selected == ()
    assert summary.detected == ()
    assert service.baselines.active().baseline_id == "base-cycle"


def test_constructor_package_records_the_exact_source_state(tmp_path):
    service = _ready_service(tmp_path)
    result = _build_and_register_stacer(service, tmp_path)

    manifest = inspect_package(result.path).manifest
    fingerprints = manifest.metadata.get("source_fingerprints")

    assert isinstance(fingerprints, dict)
    assert set(fingerprints) == {"apt:stacer"}
    assert fingerprints["apt:stacer"].startswith("sha256:")


def test_packaged_state_is_not_offered_again_after_restart(tmp_path):
    service = _ready_service(tmp_path)
    _build_and_register_stacer(service, tmp_path)

    # Una sesión nueva debe descubrir por los paquetes locales qué estados ya
    # fueron convertidos en un cambio, sin depender de memoria efímera de TUI.
    restarted = ChangeConstructorService(root=service.root, home=service.home)
    summary = restarted.summary()

    assert "apt:stacer" not in {item.change_id for item in summary.detected}


def test_same_change_reappears_if_its_observed_state_changes(tmp_path):
    service = _ready_service(tmp_path)
    _build_and_register_stacer(service, tmp_path)

    upgraded = _apt_app("stacer")
    upgraded.version = "2.0"
    inventory_mod.save_inventory(
        _inventory("current-upgraded", applications=[upgraded]),
        root=service.root,
    )

    restarted = ChangeConstructorService(root=service.root, home=service.home)
    summary = restarted.summary()

    assert "apt:stacer" in {item.change_id for item in summary.detected}


def test_removing_the_local_package_makes_the_change_pending_again(tmp_path):
    service = _ready_service(tmp_path)
    _build_and_register_stacer(service, tmp_path)
    library = PortableLibrary(root=service.root)
    library.remove_all("stacer")

    restarted = ChangeConstructorService(root=service.root, home=service.home)
    summary = restarted.summary()

    assert "apt:stacer" in {item.change_id for item in summary.detected}


def test_tui_export_success_resets_to_detection_instead_of_leaving_step_four():
    source = Path("styler/tui/app.py").read_text(encoding="utf-8")
    start = source.index("class ChangeConstructorScreen")
    end = source.index("class HistoryScreen")
    block = source[start:end]

    assert "await self._reset_after_export()" in block
    assert "self.summary = self.app.constructor.begin_next_cycle()" in block
    assert "self.step_index = 1" in block
    assert 'self.query_one("#constructor-package-id", Input).value = ""' in block
    assert 'self.query_one("#constructor-package-name", Input).value = ""' in block
