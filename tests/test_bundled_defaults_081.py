"""0.8.1: los defaults oficiales pertenecen a una identidad concreta, nunca son globales."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from styler.baselines import BaselineKind, BaselineService
from styler.baselines import store
from styler.baselines.models import BaselineDefinition, ImageIdentity
from styler.baselines.service import bundled_catalog_entries, default_baseline_id
from styler.portable import PackageType, inspect_package
from styler.provenance.models import Inventory, SystemIdentity


BASELINE_ID = "linuxmint-22.3-xfce-x11-stable-x86_64"
PACKAGE_NAME = f"{BASELINE_ID}.stylerpkg"


def mint_223_xfce(*, session: str = "x11", arch: str = "x86_64") -> SystemIdentity:
    return SystemIdentity(
        distro_id="linuxmint",
        distro_version="22.3",
        architecture=arch,
        desktop="XFCE",
        session_type=session,
        release_model="stable",
    )


def _official(baseline_id: str, system: SystemIdentity, *, created_at: float = 1.0) -> BaselineDefinition:
    return BaselineDefinition(
        baseline_id=baseline_id,
        name=baseline_id,
        kind=BaselineKind.OFFICIAL,
        inventory=Inventory(inventory_id=f"inv-{baseline_id}", system=system, scope="all"),
        image=ImageIdentity(clean_install=True),
        created_at=created_at,
        trusted=True,
        source="test-catalog",
    )


def test_linuxmint_default_is_bundled_as_one_stylerpkg():
    candidates = {item.name: item for item in bundled_catalog_entries()}
    assert PACKAGE_NAME in candidates
    assert "linuxmint-22.3-xfce-x86_64.stylerpkg" not in candidates
    inspection = inspect_package(Path(str(candidates[PACKAGE_NAME])))
    assert inspection.manifest.package_type is PackageType.BASELINE
    assert inspection.manifest.metadata["baseline_id"] == BASELINE_ID


def test_default_id_contains_platform_dimensions_that_can_differ():
    assert default_baseline_id(mint_223_xfce(), BaselineKind.OFFICIAL) == BASELINE_ID
    assert (
        default_baseline_id(mint_223_xfce(session="wayland"), BaselineKind.OFFICIAL)
        == "linuxmint-22.3-xfce-wayland-stable-x86_64"
    )


def test_bundled_mint_baseline_is_official_and_recommended_only_to_exact_target(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    service = BaselineService(tmp_path / "library", home=home)
    definition = service.get(BASELINE_ID)

    assert definition.kind is BaselineKind.OFFICIAL
    assert definition.trusted is True
    assert definition.image.clean_install is True
    assert definition.system == mint_223_xfce()
    assert definition.matches_default_identity(mint_223_xfce())
    assert service.recommended(mint_223_xfce()).baseline_id == BASELINE_ID

    incompatible = (
        replace(mint_223_xfce(), distro_id="ubuntu", distro_version="24.04"),
        replace(mint_223_xfce(), distro_version="22.2"),
        replace(mint_223_xfce(), desktop="Cinnamon"),
        replace(mint_223_xfce(), session_type="wayland"),
        replace(mint_223_xfce(), release_model="rolling"),
        replace(mint_223_xfce(), architecture="aarch64"),
        replace(mint_223_xfce(), session_type=""),
    )
    for system in incompatible:
        assert not definition.matches_default_identity(system)
        assert service.recommended(system) is None


def test_first_use_never_adopts_mint_as_a_global_fallback(tmp_path, monkeypatch):
    from styler.baselines import service as service_module

    home = tmp_path / "home"
    home.mkdir()
    ubuntu = SystemIdentity(
        distro_id="ubuntu",
        distro_version="24.04",
        architecture="x86_64",
        desktop="XFCE",
        session_type="x11",
        release_model="stable",
    )
    monkeypatch.setattr(service_module.inventory_mod, "detect_system_identity", lambda: ubuntu)
    service = BaselineService(tmp_path / "library", home=home)

    assert service.get(BASELINE_ID) is not None  # está precargada en el catálogo
    assert service.recommended() is None         # pero NO pertenece a Ubuntu
    assert service.active() is None              # y jamás se adopta como fallback
    assert service.active(auto_select=False) is None


def test_each_distro_can_have_its_own_default_in_the_same_catalog(tmp_path):
    """El selector elige por identidad, no por 'la única oficial que exista'."""
    root = tmp_path / "library"
    home = tmp_path / "home"
    home.mkdir()
    service = BaselineService(root, home=home)

    ubuntu_system = SystemIdentity(
        distro_id="ubuntu", distro_version="24.04", architecture="x86_64",
        desktop="GNOME", session_type="wayland", release_model="stable",
    )
    ubuntu = _official("ubuntu-24.04-gnome-wayland-stable-x86_64", ubuntu_system, created_at=10.0)
    store.save(ubuntu, root=root)

    mint = service.recommended(mint_223_xfce())
    assert mint is not None and mint.baseline_id == BASELINE_ID
    selected_ubuntu = service.recommended(ubuntu_system)
    assert selected_ubuntu is not None
    assert selected_ubuntu.baseline_id == ubuntu.baseline_id


def test_missing_platform_fact_does_not_guess_a_default(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    service = BaselineService(tmp_path / "library", home=home)
    incomplete = replace(mint_223_xfce(), desktop="")
    assert service.recommended(incomplete) is None


def test_upgrade_prunes_a_retired_bundled_default_instead_of_leaving_two(tmp_path):
    """0.8.0 no debe quedar viviendo como un segundo default al actualizar."""
    root = tmp_path / "library"
    home = tmp_path / "home"
    home.mkdir()
    old_id = "linuxmint-22.3-xfce-x86_64"
    old = _official(old_id, mint_223_xfce(), created_at=0.5)
    old = replace(old, source=f"bundled-catalog:{old_id}.stylerpkg")
    store.save(old, root=root)
    store.activate(old_id, root=root)

    sentinel = root / ".styler" / store.BASELINES_DIR / "bundled-catalog.json"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(
        '{"signature":"catalogo-0.8.0","baseline_ids":["linuxmint-22.3-xfce-x86_64"],"fingerprints":{}}',
        encoding="utf-8",
    )

    service = BaselineService(root, home=home)
    ids = {item.baseline_id for item in service.list()}
    assert old_id not in ids
    assert BASELINE_ID in ids
    assert service.active(auto_select=False) is None
