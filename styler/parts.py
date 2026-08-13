"""
styler.parts
===============
Clasifica una ruta portable en una «parte de una configuración»: la
unidad que el usuario final ve y elige ("Tema y colores", "Iconos",
"Paneles y widgets", "Atajos de teclado"...).

Esto NO es lo mismo que `interpreter.CATEGORY_BY_ROOT`, que agrupa
diferencias por directorio para poder *compararlas*. Aquí el objetivo
es distinto: producir partes reutilizables y combinables entre
configuraciones distintas (capas). Por eso vive en su propio módulo.

Prioridad declarada: KDE Plasma. Las rutas de otros escritorios se
reconocen, pero el estado de compatibilidad las marca como
experimentales (ver styler/compat.py) en vez de fingir soporte.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PartDefinition:
    part_id: str
    title: str          # texto visible, en español
    desktop: str        # kde | gtk | any
    patterns: tuple[str, ...]


# El orden importa: la primera coincidencia gana, así que las reglas
# más específicas (konsolerc) van antes que las genéricas (${HOME}/.config).
PART_DEFINITIONS: tuple[PartDefinition, ...] = (
    PartDefinition(
        "tema-colores", "Tema y colores", "kde",
        (
            "${HOME}/.config/kdeglobals",
            "${HOME}/.config/kdedefaults",
            "${HOME}/.config/kwinrc",
            "${HOME}/.config/plasmarc",
            "${HOME}/.local/share/color-schemes",
            "${HOME}/.local/share/plasma/desktoptheme",
            "${HOME}/.local/share/plasma/look-and-feel",
            "${HOME}/.local/share/themes",
            "${HOME}/.themes",
            "${HOME}/.config/gtk-3.0",
            "${HOME}/.config/gtk-4.0",
        ),
    ),
    PartDefinition(
        "iconos", "Iconos", "any",
        ("${HOME}/.icons", "${HOME}/.local/share/icons"),
    ),
    PartDefinition(
        "cursores", "Cursores", "any",
        ("${HOME}/.local/share/cursors", "${HOME}/.config/kcminputrc"),
    ),
    PartDefinition(
        "fuentes", "Fuentes", "any",
        ("${HOME}/.fonts", "${HOME}/.local/share/fonts", "${HOME}/.config/fontconfig"),
    ),
    PartDefinition(
        "fondos", "Fondos de pantalla", "any",
        (
            "${HOME}/.local/share/wallpapers",
            "${HOME}/.local/share/plasma/wallpapers",
            "${HOME}/Pictures/Wallpapers",
        ),
    ),
    PartDefinition(
        "paneles", "Paneles y widgets", "kde",
        (
            "${HOME}/.config/plasma-org.kde.plasma.desktop-appletsrc",
            "${HOME}/.config/plasmashellrc",
            "${HOME}/.local/share/plasma/plasmoids",
            "${HOME}/.local/share/plasma/layout-templates",
        ),
    ),
    PartDefinition(
        "atajos", "Atajos de teclado", "kde",
        (
            "${HOME}/.config/kglobalshortcutsrc",
            "${HOME}/.config/khotkeysrc",
            "${HOME}/.config/kxkbrc",
        ),
    ),
    PartDefinition(
        "konsole", "Configuración de Konsole", "kde",
        ("${HOME}/.config/konsolerc", "${HOME}/.local/share/konsole"),
    ),
    PartDefinition(
        "dolphin", "Configuración de Dolphin", "kde",
        ("${HOME}/.config/dolphinrc", "${HOME}/.local/share/dolphin"),
    ),
    PartDefinition(
        "gimp", "Configuración de GIMP", "any",
        ("${HOME}/.config/GIMP",),
    ),
    PartDefinition(
        "aplicaciones-config", "Configuración de aplicaciones", "any",
        ("${HOME}/.config", "${HOME}/.local/share/applications"),
    ),
)

OTHER_PART = PartDefinition("otros", "Otros archivos", "any", ())

# Parte especial: no agrupa rutas, agrupa aplicaciones. Nunca la devuelve
# classify() (no tiene patrones); se construye desde el inventario de
# procedencia en layers.extract_layers.
APPLICATIONS_PART = PartDefinition("aplicaciones", "Aplicaciones", "any", ())

_BY_ID = {part.part_id: part for part in PART_DEFINITIONS}
_BY_ID[OTHER_PART.part_id] = OTHER_PART
_BY_ID[APPLICATIONS_PART.part_id] = APPLICATIONS_PART


def classify(portable_path: str) -> PartDefinition:
    for part in PART_DEFINITIONS:
        for pattern in part.patterns:
            if portable_path == pattern or portable_path.startswith(pattern + "/"):
                return part
    return OTHER_PART


def part_by_id(part_id: str) -> PartDefinition | None:
    return _BY_ID.get(part_id)


def title_for(part_id: str) -> str:
    part = _BY_ID.get(part_id)
    return part.title if part else part_id


def group_by_part(paths: list[str]) -> dict[str, list[str]]:
    """Agrupa rutas portables por parte. Útil para mostrar «Esta
    configuración incluye: Tema y colores, Iconos, Paneles...»."""
    grouped: dict[str, list[str]] = {}
    for path in paths:
        grouped.setdefault(classify(path).part_id, []).append(path)
    return grouped
