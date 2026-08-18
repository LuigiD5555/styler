"""Regression tests for Linux package metadata and release tooling."""
from __future__ import annotations

import re
import subprocess
import tarfile
from pathlib import Path

import yaml

import styler

ROOT = Path(__file__).resolve().parents[1]


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match
    return match.group(1)


def test_package_versions_match_project_version():
    version = project_version()
    assert styler.__version__ == version
    assert f"pkgver={version}" in (ROOT / "packaging/arch/PKGBUILD").read_text()
    assert f"Version:        {version}" in (ROOT / "packaging/rpm/styler.spec").read_text()
    assert f"styler ({version}-1)" in (ROOT / "debian/changelog").read_text()
    assert f"pkgver={version}" in (ROOT / "packaging/release/arch/PKGBUILD").read_text()
    assert f"Version:        {version}" in (ROOT / "packaging/release/rpm/styler-portable.spec").read_text()


def test_supported_textual_range_is_shared_by_python_and_native_packages():
    pyproject = (ROOT / "pyproject.toml").read_text()
    requirements = (ROOT / "requirements.txt").read_text()
    debian = (ROOT / "debian/control").read_text()
    arch = (ROOT / "packaging/arch/PKGBUILD").read_text()
    rpm = (ROOT / "packaging/rpm/styler.spec").read_text()
    assert "textual>=0.89.1,<9" in pyproject
    assert "textual>=0.89.1,<9" in requirements
    assert "python3-textual (>= 0.89.1)" in debian
    assert "python-textual>=0.89.1" in arch
    assert "python3dist(textual) >= 0.89.1" in rpm


def test_release_runtime_is_pinned_and_python_only_by_contract():
    requirements = (ROOT / "packaging/runtime/requirements.txt").read_text()
    runtime_builder = (ROOT / "scripts/build-portable-runtime.sh").read_text()
    assert "textual==8.2.8" in requirements
    assert "PyYAML==6.0.3" in requirements
    assert "-name '*.so'" in runtime_builder
    assert "yaml.safe_load" in runtime_builder
    assert "StylerApp" in runtime_builder


def test_all_native_and_release_packages_install_desktop_mime_and_manual():
    native_deb = (ROOT / "debian/styler.install").read_text() + (ROOT / "debian/styler.manpages").read_text()
    files = [
        native_deb,
        (ROOT / "packaging/arch/PKGBUILD").read_text(),
        (ROOT / "packaging/rpm/styler.spec").read_text(),
        (ROOT / "scripts/build-release-deb.sh").read_text(),
        (ROOT / "packaging/release/arch/PKGBUILD").read_text(),
        (ROOT / "packaging/release/rpm/styler-portable.spec").read_text(),
    ]
    for text in files:
        assert "styler.desktop" in text
        assert "styler-package.xml" in text
        assert "styler.1" in text


def test_release_packages_use_the_same_runtime_location():
    deb = (ROOT / "scripts/build-release-deb.sh").read_text()
    arch = (ROOT / "packaging/release/arch/PKGBUILD").read_text()
    rpm = (ROOT / "packaging/release/rpm/styler-portable.spec").read_text()
    for text in (deb, arch, rpm):
        assert "/usr/lib/styler/styler.pyz" in text
    assert "%{_prefix}/lib/styler/styler.pyz" in rpm
    assert "%{_libdir}/styler" not in rpm


def test_package_build_scripts_have_valid_bash_syntax():
    scripts = sorted((ROOT / "scripts").glob("*.sh"))
    assert scripts
    for script in scripts:
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_release_workflow_has_one_canonical_package_build_path():
    workflow_path = ROOT / ".github/workflows/packages.yml"
    workflow = workflow_path.read_text()
    assert yaml.safe_load(workflow)
    assert "release:" in workflow
    # GitHub Actions no vuelve a reimplementar los builders: el mismo script
    # usado localmente crea runtime portable + DEB + Arch + RPM en contenedores.
    assert "STYLER_TEXTUAL_VERSION" not in workflow
    assert "build-release-in-containers.sh all" in workflow
    assert "make-source-archive.sh" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "runtime:" not in workflow
    assert "deb:" not in workflow
    assert "arch:" not in workflow
    assert "rpm:" not in workflow


def test_source_archive_contains_both_packaging_channels_without_outputs(tmp_path):
    result = subprocess.run(
        [str(ROOT / "scripts/make-source-archive.sh"), str(tmp_path)],
        check=True,
        text=True,
        capture_output=True,
    )
    archive = Path(result.stdout.strip().splitlines()[-1])
    assert archive.exists()
    with tarfile.open(archive, "r:gz") as handle:
        names = handle.getnames()
    prefix = f"styler-{project_version()}/"
    assert prefix + "debian/control" in names
    assert prefix + "packaging/arch/PKGBUILD" in names
    assert prefix + "packaging/rpm/styler.spec" in names
    assert prefix + "packaging/release/arch/PKGBUILD" in names
    assert prefix + "packaging/release/rpm/styler-portable.spec" in names
    assert prefix + "scripts/build-release-in-containers.sh" in names
    assert not any("/dist/" in name or name.endswith(".egg-info") for name in names)


def test_project_has_one_current_human_manual():
    manual = ROOT / "docs/STYLER.md"
    assert manual.is_file()
    text = manual.read_text(encoding="utf-8")
    assert "Manual único de Styler" in text
    assert "Pipeline actual de PhotoGIMP" in text
    assert "PipeCraft dentro de Styler" in text
    assert "Auditoría de los archivos del ZIP" in text
    human_docs = sorted(
        path for path in (ROOT / "docs").glob("*.md") if path.name != "STYLER.md"
    )
    assert human_docs == []


def test_release_tree_does_not_include_historical_reports_or_generated_outputs():
    forbidden = [
        "CHANGELOG.md",
        "README-INSTALAR.txt",
        "dist",
    ]
    for relative in forbidden:
        assert not (ROOT / relative).exists(), relative
    assert not list(ROOT.glob("MODIFICACION_*_LEEME.md"))
    assert not list(ROOT.glob("VALIDACION_*.txt"))
    assert not list(ROOT.glob("RELEASE_NOTES_*.md"))


def test_repository_generator_supports_all_three_package_managers_and_signing():
    script = (ROOT / "scripts/create-repositories.sh").read_text()
    assert "dpkg-scanpackages" in script
    assert "repo-add" in script
    assert "createrepo_c" in script
    assert "STYLER_GPG_KEY" in script
    assert "gpg" in script


def test_public_metadata_can_be_configured_without_inventing_a_url():
    script = (ROOT / "scripts/configure-packaging.py").read_text()
    assert "--name" in script
    assert "--email" in script
    assert "--url" in script
    assert re.search(r"example\.invalid", (ROOT / "debian/control").read_text())


# --------------------------------------------------------------------------- #
# Catálogo de componentes 0.13: debe viajar dentro del paquete instalado.
# Sin estas comprobaciones, un wheel podría instalarse sin catálogo y Styler
# arrancaría sin saber qué es GIMP.
# --------------------------------------------------------------------------- #

def test_package_data_incluye_el_catalogo_de_componentes():
    import tomllib
    from pathlib import Path

    with Path("pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    patterns = data["tool"]["setuptools"]["package-data"]["styler"]
    assert any("catalog/components" in pattern and pattern.endswith("*.toml") for pattern in patterns)
    assert any("assets" in pattern for pattern in patterns), "los assets (catalog://) deben empaquetarse"


def test_todos_los_componentes_del_indice_existen_en_disco():
    import tomllib
    from pathlib import Path

    root = Path("styler/catalog/components")
    with (root / "index.toml").open("rb") as handle:
        index = tomllib.load(handle)
    for component_id, relative in index["components"].items():
        assert (root / relative).is_file(), f"{component_id} apunta a un archivo inexistente: {relative}"


def test_el_asset_de_photogimp_existe():
    from pathlib import Path

    asset = Path("styler/catalog/components/assets/photogimp")
    assert asset.is_dir()
    assert (asset / ".photogimp-marker").is_file()
