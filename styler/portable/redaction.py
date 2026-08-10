"""Qué no debe salir nunca de este equipo dentro de un paquete.

Se aplica antes de guardar, no antes de compartir: un paquete guardado con un
token dentro ya puede compartirse por accidente.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Iterable

HOME_TOKEN = "${HOME}"

#: Rutas que no representan personalización, sino estado, caché o secretos.
SENSITIVE_PATTERNS: tuple[str, ...] = (
    "*/.ssh/*",
    "*/.gnupg/*",
    "*/keyrings/*",
    "*/.password-store/*",
    "*token*",
    "*secret*",
    "*credential*",
    "*password*",
    "*cookies*",
    "*.pem",
    "*.key",
    "*/.aws/*",
    "*/.docker/config.json",
)

#: Rutas volátiles: cambian solas y ensucian cualquier grabación.
VOLATILE_PATTERNS: tuple[str, ...] = (
    "*/cache/*",
    "*/Cache/*",
    "*/.cache/*",
    "*/thumbnails/*",
    "*/recently-used*",
    "*/RecentDocuments/*",
    "*/Trash/*",
    "*/tmp/*",
    "*/gvfs*",
    "*.lock",
    "*.log",
    "*.tmp",
    "*.swp",
    "*~",
    "*/systemd/*",
    "*/dbus-1/*",
    "*/.local/state/*",
    "*sessionstore*",
    "*/session/*",
    "*history*",
)

_SECRET_VALUE = re.compile(
    r"(?i)\b(token|secret|api[_-]?key|password|passwd|bearer)\b(\s*[:=]\s*)(\S+)"
)


#: Nombres de carpeta que nunca son personalización, aparezcan donde aparezcan.
VOLATILE_COMPONENTS: frozenset[str] = frozenset(
    {
        "cache",
        ".cache",
        "caches",
        "thumbnails",
        "trash",
        "tmp",
        "temp",
        "gvfs",
        "state",
        "systemd",
        "dbus-1",
        "recently-used",
        "crashreports",
    }
)


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    lowered = path.lower()
    return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in patterns)


def is_sensitive(path: str | Path) -> bool:
    return matches_any(str(path), SENSITIVE_PATTERNS)


def is_volatile(path: str | Path) -> bool:
    raw = str(path)
    components = {part.lower() for part in Path(raw).parts}
    if components & VOLATILE_COMPONENTS:
        return True
    return matches_any(raw, VOLATILE_PATTERNS)


def should_record(path: str | Path) -> bool:
    """Solo se graba lo que es personalización real y no es un secreto."""
    return not is_sensitive(path) and not is_volatile(path)



_SENSITIVE_OPTION_NAMES = frozenset({
    "--token", "--secret", "--api-key", "--apikey", "--password",
    "--passwd", "--bearer", "--credential", "--credentials",
})

def redact_argv(argv: Iterable[str]) -> tuple[str, ...]:
    """Redacta secretos aunque la clave y el valor sean argumentos separados."""
    values = [str(item) for item in argv]
    result: list[str] = []
    redact_next = False
    for raw in values:
        if redact_next:
            result.append("***")
            redact_next = False
            continue
        lowered = raw.strip().lower()
        name, sep, _value = lowered.partition("=")
        if name in _SENSITIVE_OPTION_NAMES:
            if sep:
                result.append(raw.split("=", 1)[0] + "=***")
            else:
                result.append(raw)
                redact_next = True
            continue
        result.append(redact_text(raw))
    return tuple(result)


def portable_path(path: str | Path, home: str | Path) -> str:
    """Sustituye el HOME real por un marcador.

    Sin esto, cualquier paquete exportado revela el nombre de usuario y no
    funciona en el equipo de otra persona.
    """
    raw = str(path)
    home_str = str(Path(home))
    if raw == home_str:
        return HOME_TOKEN
    if raw.startswith(home_str.rstrip("/") + "/"):
        return HOME_TOKEN + raw[len(home_str.rstrip("/")):]
    return raw


def redact_text(text: str) -> str:
    """Oculta el valor de cualquier pareja clave/valor que parezca un secreto."""
    return _SECRET_VALUE.sub(lambda match: f"{match.group(1)}{match.group(2)}***", text)
