"""Datos sintéticos para `styler --demo`.

Nunca lee ni modifica la configuración real: crea objetos inventados dentro de
una biblioteca temporal. Sirve para diseñar, tomar capturas y hacer pruebas de
usabilidad sin arriesgar el equipo de nadie.
"""
from __future__ import annotations

import time
from pathlib import Path

from styler.models import FileEntry, Package, State
from styler.objectstore import ObjectStore
from styler.snapshot import Snapshot

DEMO_FILES: tuple[tuple[str, str, int], ...] = (
    ("${HOME}/.config/kdeglobals", "[General]\nColorScheme=BreezeDark\n", 4200),
    ("${HOME}/.config/kwinrc", "[Windows]\nBorderlessMaximized=true\n", 1800),
    ("${HOME}/.local/share/icons/Papirus/index.theme", "[Icon Theme]\nName=Papirus\n", 900),
    ("${HOME}/.icons/custom/folder.svg", "<svg><!-- icono --></svg>\n", 2400),
    ("${HOME}/.config/plasma-org.kde.plasma.desktop-appletsrc", "[Containments][1]\n", 12000),
    ("${HOME}/.config/kglobalshortcutsrc", "[kwin]\nSwitch One Desktop=Meta+1\n", 3100),
    ("${HOME}/.config/konsolerc", "[Desktop Entry]\nDefaultProfile=Oscuro\n", 700),
    ("${HOME}/.cache/icon-cache.kcache", "cache", 500),          # se marcará: no recomendado
    ("${HOME}/.local/share/desconocido/state.bin", "?", 620),      # se marcará: revisar
)


def synthetic_snapshot(root: str, label: str = "Demo", scope: str = "plasma") -> Snapshot:
    store = ObjectStore(root=root)
    staging = Path(root) / "demo-src"
    staging.mkdir(parents=True, exist_ok=True)

    entries: list[FileEntry] = []
    for index, (path, content, size) in enumerate(DEMO_FILES):
        source = staging / f"demo-{index}"
        source.write_text(content, encoding="utf-8")
        checksum, _ = store.store_file(source)
        entries.append(FileEntry(path=path, checksum=checksum, size=size, mode="0644"))

    return Snapshot(
        snapshot_id=f"demo-{int(time.time())}",
        label=label,
        origin="manual",
        state=State(
            state_id=f"demo-state-{int(time.time())}",
            label=label,
            desktops=["kde"],
            distro="Demo Linux",
            packages=[Package(manager="apt", name="papirus-icon-theme")],
            files=entries,
        ),
    )


def demo_snapshot_factory(root: str):
    def factory(label: str, scope: str) -> Snapshot:
        return synthetic_snapshot(root, label=label, scope=scope)
    return factory
