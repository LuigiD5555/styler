"""El texto humano se normaliza al crear paquetes; lo importado sigue siendo estricto."""
from pathlib import Path

import pytest

from styler.baselines import BaselineKind
from styler.portable import PortablePackageError, inspect_package, normalize_identifier, validate_identifier
from styler.provenance import inventory as inventory_mod
from styler.ui.constructor import ChangeConstructorService
from tests.test_change_constructor_070 import _apt_app, _inventory


def _ready_service(tmp_path: Path) -> ChangeConstructorService:
    root = tmp_path / "library"
    service = ChangeConstructorService(root=root, home=tmp_path / "home")
    service.baselines.register_inventory(
        _inventory("base-normalize"),
        kind=BaselineKind.CUSTOM,
        baseline_id="base-normalize",
        name="Base",
        activate_after=True,
    )
    inventory_mod.save_inventory(
        _inventory("current-normalize", applications=[_apt_app("stacer")]),
        root=root,
    )
    service.select_all_exportable()
    return service


def test_human_identifier_is_normalized() -> None:
    assert normalize_identifier("Local") == "local"
    assert normalize_identifier("  Mi paquete José 2026  ") == "mi-paquete-jose-2026"
    assert normalize_identifier("Tema / Cursor + CSS") == "tema-cursor-css"


def test_external_identifier_validation_remains_strict() -> None:
    with pytest.raises(PortablePackageError):
        validate_identifier("Local", "identificador de paquete")


def test_constructor_normalizes_id_before_generating_plan(tmp_path: Path) -> None:
    service = _ready_service(tmp_path)
    plan = service.generated_plan("Local", "Local")
    assert plan.package_id == "local"
    assert plan.graph.graph_id.startswith("local")


def test_build_package_uses_normalized_id_and_filename_can_be_human_input(tmp_path: Path) -> None:
    service = _ready_service(tmp_path)
    result = service.build_package(tmp_path / "out", "Mi Paquete José", "Mi Paquete José")
    package = inspect_package(result.path)
    assert package.manifest.package_id == "mi-paquete-jose"
    assert package.manifest.name == "Mi Paquete José"
    assert Path(result.path).suffix == ".stylerpkg"
