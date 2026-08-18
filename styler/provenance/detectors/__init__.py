"""Registro único de detectores de aplicaciones instaladas."""
from pathlib import Path

from styler.provenance.detectors.appimage import AppImageDetector
from styler.provenance.detectors.apt import AptDetector
from styler.provenance.detectors.base import CommandError, CommandRunner, Detector, Runner
from styler.provenance.detectors.brew import BrewDetector
from styler.provenance.detectors.containers import ContainerDetector
from styler.provenance.detectors.flatpak import FlatpakDetector
from styler.provenance.detectors.language_tools import LanguageToolDetector
from styler.provenance.detectors.manual_binaries import ManualBinaryDetector
from styler.provenance.detectors.nix import NixDetector
from styler.provenance.detectors.pacman import PacmanDetector
from styler.provenance.detectors.rpm import RpmDetector
from styler.provenance.detectors.snap import SnapDetector
from styler.provenance.detectors.zypper import ZypperDetector


def all_detectors(runner: Runner | None = None, *, home: str | Path | None = None) -> list[Detector]:
    """Todos los detectores locales; cada uno decide si aplica en la máquina."""
    home_path = Path(home or Path.home()).expanduser()
    return [
        AptDetector(runner),
        FlatpakDetector(runner),
        SnapDetector(runner),
        PacmanDetector(runner),
        RpmDetector(runner),
        AppImageDetector(
            runner,
            search_dirs=[
                home_path / "Applications",
                home_path / ".local/bin",
                home_path / "AppImages",
                home_path / "Apps",
                home_path / "bin",
                Path("/opt"),
            ],
        ),
        ZypperDetector(runner),
        NixDetector(runner, home=home_path),
        BrewDetector(runner),
        LanguageToolDetector(runner, home=home_path),
        ContainerDetector(runner),
        ManualBinaryDetector(
            runner,
            directories=(
                str(home_path / ".local/bin"),
                str(home_path / "bin"),
                "/usr/local/bin",
                "/opt",
            ),
        ),
    ]


__all__ = [
    "AppImageDetector", "AptDetector", "BrewDetector", "ContainerDetector",
    "CommandError", "CommandRunner", "Detector", "FlatpakDetector",
    "LanguageToolDetector", "ManualBinaryDetector", "NixDetector", "PacmanDetector",
    "RpmDetector", "Runner", "SnapDetector", "ZypperDetector", "all_detectors",
]
