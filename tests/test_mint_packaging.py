from __future__ import annotations

import subprocess
import sys
import zipfile
import shutil
from pathlib import Path

from styler import catalogs
from styler.target import detect_target, resolve_desktop


def mint_file(path: Path):
    path.write_text(
        'NAME="Linux Mint"\nID=linuxmint\nID_LIKE="ubuntu debian"\n'
        'VERSION_ID="22.3"\nVERSION_CODENAME=zena\nUBUNTU_CODENAME=noble\n'
        'PRETTY_NAME="Linux Mint 22.3"\n', encoding="utf-8"
    )


def test_linux_mint_fields_and_ubuntu_family(tmp_path: Path):
    os_release = tmp_path / "os-release"
    mint_file(os_release)
    catalogs.clear_cache()
    target = detect_target(os_release)
    assert target.family == "ubuntu"
    assert target.native_manager == "apt"
    assert target.version_codename == "zena"
    assert target.ubuntu_codename == "noble"
    assert resolve_desktop("kde-plasma", target) == ("apt", "kde-plasma-desktop")


def test_wheel_contains_catalogs(tmp_path: Path):
    project = Path(__file__).parents[1]
    out = tmp_path / "dist"
    try:
        subprocess.run([sys.executable, "-m", "pip", "wheel", str(project), "--no-deps", "--no-build-isolation", "-w", str(out)], check=True, capture_output=True, text=True)
        wheel = next(out.glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
        assert "styler/catalog/distros.toml" in names
        assert "styler/catalog/desktops.toml" in names
        assert "styler/changes/models.py" in names
        assert "styler/changes/service.py" in names
        assert "styler/tui/styles/screens.tcss" in names
    finally:
        shutil.rmtree(project / "build", ignore_errors=True)
        shutil.rmtree(project / "styler_linux.egg-info", ignore_errors=True)
