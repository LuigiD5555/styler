"""Observa archivos según un alcance explícito y conservador.

La captura recomendada de Plasma usa una lista permitida. Deliberadamente no
recorre todo ``~/.config`` y tampoco copia temas descargados: Plasma se registra
como entorno instalable y los temas pueden volver a obtenerse de su proveedor.
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

from styler.hashing import hash_tree
from styler.models import FileEntry
from styler.observers.base import BaseObserver
from styler.security import is_excluded, normalize_path

class HomeRelativeRoots(Sequence[str]):
    """Secuencia que resuelve ``Path.home()`` al usarla, no al importarla.

    Esto elimina el estado global dependiente del orden de importación. Es
    importante para pruebas, ejecución con otro usuario y futuras llamadas al
    motor Rust con un ``HostContext`` explícito.
    """

    def __init__(self, relative_paths: Sequence[str]) -> None:
        self._relative_paths = tuple(relative_paths)

    def _resolved(self) -> tuple[str, ...]:
        home = Path.home()
        return tuple(str(home / relative) for relative in self._relative_paths)

    def __len__(self) -> int:
        return len(self._relative_paths)

    def __getitem__(self, index):
        return self._resolved()[index]

    def __iter__(self) -> Iterator[str]:
        return iter(self._resolved())


PLASMA_CONFIG_FILES = (
    "kdeglobals",
    "kcminputrc",
    "kglobalshortcutsrc",
    "khotkeysrc",
    "kxkbrc",
    "kwinrc",
    "plasmarc",
    "plasmashellrc",
    "plasma-org.kde.plasma.desktop-appletsrc",
    "konsolerc",
    "dolphinrc",
)

PLASMA_ROOTS = HomeRelativeRoots(
    [f".config/{name}" for name in PLASMA_CONFIG_FILES]
    + [
        ".config/kdedefaults",
        ".config/gtk-3.0",
        ".config/gtk-4.0",
        ".config/fontconfig",
        ".local/share/plasma/plasmoids",
        ".local/share/plasma/layout-templates",
        ".local/share/plasma/wallpapers",
        ".local/share/icons",
        ".local/share/cursors",
        ".local/share/fonts",
        ".local/share/wallpapers",
        ".local/share/konsole",
        ".local/share/dolphin",
        ".icons",
        ".fonts",
    ]
)

USER_APPLICATION_ROOTS = HomeRelativeRoots(
    [
        ".config/GIMP",
        ".local/share/applications",
        ".local/bin",
        ".var/app/org.gimp.GIMP/config/GIMP",
    ]
)

SYSTEM_ROOTS = ["/opt", "/usr/local", "/etc/xdg"]
DEFAULT_ROOTS = PLASMA_ROOTS

CAPTURE_SCOPES = {
    "plasma": PLASMA_ROOTS,
    "user-applications": USER_APPLICATION_ROOTS,
    "system-additions": SYSTEM_ROOTS,
}

# Compatibilidad con llamadas internas de la versión 0.5. La TUI 0.6 ya usa
# únicamente los identificadores canónicos.
SCOPE_ALIASES = {
    "user": "user-applications",
    "system": "system-additions",
}


class FilesObserver(BaseObserver):
    name = "files"

    def __init__(self, roots: list[str] | None = None, scope: str = "plasma"):
        canonical_scope = SCOPE_ALIASES.get(scope, scope)
        if roots is not None:
            self.roots = list(roots)
        else:
            if canonical_scope not in CAPTURE_SCOPES:
                raise ValueError(f"Alcance de captura desconocido: {scope}")
            self.roots = list(CAPTURE_SCOPES[canonical_scope])
        self.scope = canonical_scope

    def files(self) -> list[FileEntry]:
        return self.safe_run(self._read_files)

    def _read_files(self) -> list[FileEntry]:
        if not self.roots:
            return []
        raw_entries = hash_tree(self.roots)
        cleaned: list[FileEntry] = []
        seen: set[str] = set()
        for entry in raw_entries:
            if is_excluded(entry.path):
                continue
            entry.path = normalize_path(entry.path)
            if entry.path in seen:
                continue
            seen.add(entry.path)
            entry.owner_hint = "user" if entry.path.startswith("${HOME}") else "system"
            cleaned.append(entry)
        return cleaned
