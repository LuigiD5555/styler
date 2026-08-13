"""Vocabulario visual de Styler.

Los iconos se usan como apoyo de reconocimiento, nunca como sustituto del
texto. De esta forma una persona puede asociar acciones con experiencias ya
conocidas (guardar, importar, deshacer, eliminar) sin convertir la interfaz en
una colección informal de dibujos.

Modos disponibles mediante ``STYLER_ICON_MODE``:

``emoji`` (predeterminado)
    Emojis comunes y fáciles de reconocer.
``symbols``
    Símbolos Unicode sobrios para fuentes sin emoji a color.
``text``
    Sin iconos; conserva únicamente las etiquetas.
"""
from __future__ import annotations

import locale
import os
import sys


EMOJI_ICONS: dict[str, str] = {
    # Secciones y conceptos
    "home": "🏠",
    "capture": "📥",
    "library": "📚",
    "origins": "📦",
    "history": "🕘",
    "help": "❓",
    "appearance": "🎨",
    "applications": "📦",
    "advanced": "⚙",
    "selection": "☑",
    "profile": "🖥",
    "import": "📥",
    "export": "📤",
    "search": "🔎",
    "warning": "⚠",
    "success": "✅",
    "security": "🛡",
    "example": "💡",
    "information": "ℹ",
    # Acciones
    "start": "▶",
    "next": "→",
    "back": "←",
    "cancel": "✕",
    "close": "✕",
    "save": "💾",
    "review": "🔍",
    "analyze": "🔎",
    "apply": "✅",
    "undo": "↩",
    "refresh": "🔄",
    "clear": "🧹",
    "delete": "🗑",
    "combine": "🔗",
    "settings": "⚙",
    "folder": "📁",
    "file": "📄",
    "keyboard": "⌨",
    "restore": "🛠",
    "more": "⋯",
    "finish": "✓",
    "details": "ℹ",
    "permissions": "🔐",
}


SYMBOL_ICONS: dict[str, str] = {
    "home": "⌂",
    "capture": "↓",
    "library": "▤",
    "origins": "▦",
    "history": "◷",
    "help": "?",
    "appearance": "◈",
    "applications": "▦",
    "advanced": "⚙",
    "selection": "☑",
    "profile": "▣",
    "import": "↓",
    "export": "↑",
    "search": "⌕",
    "warning": "!",
    "success": "✓",
    "security": "◆",
    "example": "•",
    "information": "i",
    "start": "▶",
    "next": "→",
    "back": "←",
    "cancel": "×",
    "close": "×",
    "save": "▣",
    "review": "⌕",
    "analyze": "⌕",
    "apply": "✓",
    "undo": "↶",
    "refresh": "↻",
    "clear": "○",
    "delete": "×",
    "combine": "⇄",
    "settings": "⚙",
    "folder": "▰",
    "file": "▤",
    "keyboard": "⌨",
    "restore": "↺",
    "more": "⋯",
    "finish": "✓",
    "details": "i",
    "permissions": "◆",
}

ICON_ALIASES: dict[str, str] = {
    "scan-all": "analyze",
    "baseline": "history",
    "vault": "save",
    "policy": "settings",
    "forget": "delete",
}


def icon_mode() -> str:
    """Devuelve el modo de iconos efectivo sin asumir soporte de fuente."""

    requested = os.environ.get("STYLER_ICON_MODE", "").strip().lower()
    if requested in {"emoji", "symbols", "text"}:
        return requested

    encoding = (
        getattr(sys.__stdout__, "encoding", "")
        or locale.getpreferredencoding(False)
        or ""
    ).lower()
    if "utf" not in encoding:
        return "text"
    return "emoji"


def icon(name: str) -> str:
    """Obtiene un glifo semántico con un respaldo seguro."""

    mode = icon_mode()
    if mode == "text":
        return ""
    collection = EMOJI_ICONS if mode == "emoji" else SYMBOL_ICONS
    resolved = ICON_ALIASES.get(name, name)
    return collection.get(resolved, SYMBOL_ICONS.get(resolved, ""))


def icon_label(name: str, text: str) -> str:
    """Anteponer un icono sin quitar nunca la etiqueta comprensible."""

    glyph = icon(name)
    return f"{glyph}  {text}" if glyph else text


def action_icon(text: str, default: str = "") -> str:
    """Infiere el icono de una acción a partir de su verbo visible.

    Solo se usa para botones genéricos y confirmaciones. Las acciones centrales
    deben preferir una clave explícita para evitar ambigüedades.
    """

    lowered = text.casefold()
    rules: tuple[tuple[tuple[str, ...], str], ...] = (
        (("cancel",), "cancel"),
        (("cerrar",), "close"),
        (("entendido", "terminar"), "finish"),
        (("deshacer", "volver al estado", "regresar"), "undo"),
        (("eliminar", "quitar", "vaciar", "olvidar"), "delete"),
        (("guardar", "crear"), "save"),
        (("importar",), "import"),
        (("exportar",), "export"),
        (("aplicar", "instalar"), "apply"),
        (("analizar",), "analyze"),
        (("revisar", "buscar", "resolver"), "review"),
        (("elegir carpeta",), "folder"),
        (("elegir archivo",), "file"),
        (("ruta manual", "escribir una ruta"), "keyboard"),
        (("combinar",), "combine"),
        (("restablecer", "recargar"), "refresh"),
        (("limpiar", "deseleccionar"), "clear"),
        (("opciones", "permisos", "política"), "settings"),
        (("atrás", "volver"), "back"),
        (("continuar", "comenzar"), "next"),
        (("más",), "more"),
        (("detalle",), "details"),
    )
    for words, icon_name in rules:
        if any(word in lowered for word in words):
            return icon_name
    return default


def action_label(text: str, icon_name: str = "") -> str:
    """Etiqueta de botón consistente, con icono explícito o inferido."""

    return icon_label(icon_name or action_icon(text), text)
