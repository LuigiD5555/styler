"""Detección acotada de temas, iconos, cursores, fondos, fuentes y CSS."""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Iterable

from styler.provenance.models import ArtifactKind, SystemArtifactRecord

MAX_FILES_PER_ARTIFACT = 10_000
MAX_BYTES_PER_ARTIFACT = 256 * 1024 * 1024


def portable_path(path: Path, home: Path | None = None) -> str:
    home = (home or Path.home()).resolve()
    resolved = path.expanduser().resolve()
    try:
        return "${HOME}/" + resolved.relative_to(home).as_posix()
    except ValueError:
        return resolved.as_posix()


def _file_hash(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            total += len(chunk)
            if total > MAX_BYTES_PER_ARTIFACT:
                raise ValueError("supera el límite de tamaño")
            digest.update(chunk)
    return "sha256:" + digest.hexdigest(), total


def checksum_path(path: str | Path) -> tuple[str, int, int, bool]:
    source = Path(path).expanduser()
    if source.is_symlink():
        raise ValueError("no se siguen enlaces simbólicos")
    if source.is_file():
        checksum, size = _file_hash(source)
        return checksum, size, 1, False
    if not source.is_dir():
        raise ValueError("la ruta no existe")
    digest = hashlib.sha256()
    total = 0
    count = 0
    for item in sorted(source.rglob("*"), key=lambda p: p.as_posix()):
        if item.is_symlink() or not item.is_file():
            continue
        count += 1
        if count > MAX_FILES_PER_ARTIFACT:
            raise ValueError("supera el límite de archivos")
        checksum, size = _file_hash(item)
        total += size
        if total > MAX_BYTES_PER_ARTIFACT:
            raise ValueError("supera el límite de tamaño")
        relative = item.relative_to(source).as_posix()
        mode = stat.S_IMODE(item.stat().st_mode)
        digest.update(relative.encode("utf-8"))
        digest.update(checksum.encode("ascii"))
        digest.update(str(mode).encode("ascii"))
    return "sha256:" + digest.hexdigest(), total, count, True


def _record(path: Path, kind: ArtifactKind, *, home: Path) -> SystemArtifactRecord | None:
    if not path.exists() or path.is_symlink():
        return None
    try:
        checksum, size, count, is_directory = checksum_path(path)
        mode = stat.S_IMODE(path.stat().st_mode)
    except (OSError, ValueError):
        return None
    portable = portable_path(path, home)
    artifact_id = f"artifact:{kind.value}:{portable}"
    scope = "user" if portable.startswith("${HOME}") else "system"
    return SystemArtifactRecord(
        artifact_id=artifact_id,
        kind=kind,
        name=path.name,
        path=portable,
        checksum=checksum,
        size=size,
        mode=mode,
        is_directory=is_directory,
        file_count=count,
        scope=scope,
    )


def _children(roots: Iterable[Path], kind: ArtifactKind, home: Path) -> list[SystemArtifactRecord]:
    found: list[SystemArtifactRecord] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            record = _record(child, kind, home=home)
            if record is not None:
                if kind is ArtifactKind.ICON_THEME and (child / "cursors").is_dir():
                    record.kind = ArtifactKind.CURSOR_THEME
                    record.artifact_id = f"artifact:{record.kind.value}:{record.path}"
                found.append(record)
    return found


def scan_visual_artifacts(home: str | Path | None = None, runner=None) -> tuple[list[SystemArtifactRecord], list[str]]:
    home_path = Path(home or Path.home()).expanduser().resolve()
    records: list[SystemArtifactRecord] = []
    problems: list[str] = []
    specs = [
        (ArtifactKind.THEME, [home_path / ".themes", home_path / ".local/share/themes", Path("/usr/local/share/themes")]),
        (ArtifactKind.ICON_THEME, [home_path / ".icons", home_path / ".local/share/icons", Path("/usr/local/share/icons")]),
        (ArtifactKind.WALLPAPER, [home_path / ".local/share/backgrounds", home_path / "Pictures/Wallpapers", Path("/usr/local/share/backgrounds")]),
        (ArtifactKind.FONT, [home_path / ".fonts", home_path / ".local/share/fonts", Path("/usr/local/share/fonts")]),
    ]
    for kind, roots in specs:
        try:
            records.extend(_children(roots, kind, home_path))
        except OSError as exc:
            problems.append(f"No se pudo inspeccionar {kind.human}: {exc}")
    fixed = [
        home_path / ".config/gtk-3.0/gtk.css",
        home_path / ".config/gtk-4.0/gtk.css",
        home_path / ".config/waybar/style.css",
        home_path / ".config/wofi/style.css",
    ]
    dynamic: list[Path] = []
    for directory, pattern in (
        (home_path / ".config/rofi", "*.rasi"),
        (home_path / ".config/eww", "*.scss"),
        (home_path / ".config/ags", "*.css"),
    ):
        if directory.is_dir():
            dynamic.extend(directory.glob(pattern))
    for path in [*fixed, *dynamic]:
        record = _record(path, ArtifactKind.CSS, home=home_path)
        if record is not None:
            records.append(record)
    settings, setting_problems = scan_visual_settings(runner=runner)
    records.extend(settings)
    problems.extend(setting_problems)
    unique = {item.artifact_id: item for item in records}
    return sorted(unique.values(), key=lambda item: (item.kind.value, item.name.lower(), item.path)), problems

_GSETTINGS_VISUAL_KEYS = (
    ("org.gnome.desktop.interface", "gtk-theme", "Tema GTK"),
    ("org.gnome.desktop.interface", "icon-theme", "Tema de iconos"),
    ("org.gnome.desktop.interface", "cursor-theme", "Tema del cursor"),
    ("org.gnome.desktop.interface", "cursor-size", "Tamaño del cursor"),
    ("org.gnome.desktop.interface", "color-scheme", "Esquema de color"),
    ("org.gnome.desktop.background", "picture-uri", "Fondo de escritorio"),
    ("org.gnome.desktop.background", "picture-uri-dark", "Fondo oscuro"),
    ("org.cinnamon.desktop.interface", "gtk-theme", "Tema GTK de Cinnamon"),
    ("org.cinnamon.desktop.interface", "icon-theme", "Tema de iconos de Cinnamon"),
    ("org.cinnamon.desktop.interface", "cursor-theme", "Cursor de Cinnamon"),
    ("org.cinnamon.desktop.background", "picture-uri", "Fondo de Cinnamon"),
)

_KCONFIG_VISUAL_KEYS = (
    ("kdeglobals", "General", "ColorScheme", "Esquema de color de Plasma"),
    ("kdeglobals", "KDE", "widgetStyle", "Estilo de controles de Plasma"),
    ("kdeglobals", "Icons", "Theme", "Tema de iconos de Plasma"),
    ("kcminputrc", "Mouse", "cursorTheme", "Cursor de Plasma"),
    ("kcminputrc", "Mouse", "cursorSize", "Tamaño del cursor de Plasma"),
    ("plasmarc", "Theme", "name", "Tema de Plasma"),
)


def _setting_checksum(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def scan_visual_settings(runner=None) -> tuple[list[SystemArtifactRecord], list[str]]:
    """Lee ajustes visuales estructurados sin copiar bases de configuración completas."""
    if runner is None:
        from styler.provenance.detectors.base import CommandRunner
        runner = CommandRunner()
    records: list[SystemArtifactRecord] = []
    problems: list[str] = []

    if runner.available("gsettings"):
        for schema, key, title in _GSETTINGS_VISUAL_KEYS:
            try:
                value = runner.run(["gsettings", "get", schema, key]).strip()
            except Exception:
                continue
            if not value:
                continue
            path = f"setting://gsettings/{schema}/{key}"
            records.append(
                SystemArtifactRecord(
                    artifact_id=f"setting:gsettings:{schema}:{key}",
                    kind=ArtifactKind.SETTING,
                    name=title,
                    path=path,
                    checksum=_setting_checksum("gsettings", schema, key, value),
                    size=len(value.encode("utf-8")),
                    mode=0,
                    is_directory=False,
                    file_count=0,
                    scope="user",
                    setting_backend="gsettings",
                    setting_schema=schema,
                    setting_key=key,
                    setting_value=value,
                )
            )

    kread = "kreadconfig6" if runner.available("kreadconfig6") else (
        "kreadconfig5" if runner.available("kreadconfig5") else ""
    )
    if kread:
        for filename, group, key, title in _KCONFIG_VISUAL_KEYS:
            try:
                value = runner.run(
                    [kread, "--file", filename, "--group", group, "--key", key]
                ).strip()
            except Exception:
                continue
            if not value:
                continue
            path = f"setting://kconfig/{filename}/{group}/{key}"
            records.append(
                SystemArtifactRecord(
                    artifact_id=f"setting:kconfig:{filename}:{group}:{key}",
                    kind=ArtifactKind.SETTING,
                    name=title,
                    path=path,
                    checksum=_setting_checksum("kconfig", filename, group, key, value),
                    size=len(value.encode("utf-8")),
                    mode=0,
                    is_directory=False,
                    file_count=0,
                    scope="user",
                    setting_backend="kconfig",
                    setting_schema=filename,
                    setting_group=group,
                    setting_key=key,
                    setting_value=value,
                )
            )
    return records, problems
