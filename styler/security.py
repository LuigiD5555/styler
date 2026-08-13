"""Filtros de privacidad y normalización de rutas."""
from __future__ import annotations

import os
import re
from typing import Callable
from dataclasses import dataclass

from styler.validation import ValidationError, validate_logical_path

HOME = os.path.expanduser("~")

EXCLUDED_PATTERNS = [
    r"/\.ssh/", r"/\.gnupg/", r"/\.password-store/",
    r"/\.mozilla/.*/(cookies|places)\.sqlite",
    r"/\.config/google-chrome/.*/(Cookies|Login Data)",
    r"/\.bash_history$", r"/\.zsh_history$", r"/\.local/share/keyrings/",
    r"/\.cache/", r"/\.thumbnails/", r"\.log$",
    r"/NetworkManager/system-connections/", r"/\.netrc$",
    r"id_rsa", r"id_ed25519", r"\.pem$", r"\.key$",
]
_EXCLUDED_RE = [re.compile(pattern, re.IGNORECASE) for pattern in EXCLUDED_PATTERNS]

SUSPICIOUS_NAME_PATTERNS = [
    r"secret", r"token", r"passwd", r"password", r"credential",
    r"session", r"cookie", r"auth", r"\.sqlite$", r"\.db$",
]
_SUSPICIOUS_NAME_RE = [re.compile(pattern, re.IGNORECASE) for pattern in SUSPICIOUS_NAME_PATTERNS]
LARGE_FILE_BYTES = 50 * 1024 * 1024


def is_excluded(path: str) -> bool:
    return any(pattern.search(path) for pattern in _EXCLUDED_RE)


@dataclass
class Finding:
    path: str
    reason: str
    human_message: str
    severity: str = "blocking"

    @staticmethod
    def suspicious_name(path: str) -> "Finding":
        return Finding(
            path=path,
            reason="SUSPICIOUS_NAME",
            human_message="Styler encontró una ruta que podría contener información privada.",
        )

    @staticmethod
    def too_large(path: str, size: int) -> "Finding":
        return Finding(
            path=path,
            reason=f"FILE_TOO_LARGE:{size}",
            human_message="Styler encontró un archivo muy grande que probablemente no pertenece a la personalización.",
        )

    @staticmethod
    def suspicious_content(path: str) -> "Finding":
        return Finding(
            path=path,
            reason="SUSPICIOUS_CONTENT",
            human_message=(
                "Styler encontró una posible contraseña, token o llave dentro del archivo. "
                "Revísalo y elimina el dato privado antes de compartirlo."
            ),
        )

    @staticmethod
    def outside_allowed_area(path: str) -> "Finding":
        return Finding(
            path=path,
            reason="PATH_OUTSIDE_ALLOWED_AREA",
            human_message="Styler encontró una ruta fuera del área personal administrada.",
        )


def scan_entries(entries) -> list[Finding]:
    """Busca señales conocidas; ausencia de hallazgos no es garantía absoluta."""
    findings: list[Finding] = []
    for entry in entries:
        path = str(entry.path)
        try:
            validate_logical_path(path)
        except ValidationError:
            findings.append(Finding.outside_allowed_area(path))
            continue
        if is_excluded(path):
            findings.append(Finding.suspicious_name(path))
            continue
        basename = path.rsplit("/", 1)[-1]
        if any(pattern.search(basename) for pattern in _SUSPICIOUS_NAME_RE):
            findings.append(Finding.suspicious_name(path))
            continue
        if int(getattr(entry, "size", 0) or 0) > LARGE_FILE_BYTES:
            findings.append(Finding.too_large(path, int(entry.size)))
    return findings


def normalize_path(path: str) -> str:
    home = os.path.normpath(HOME)
    normalized = os.path.normpath(path)
    if normalized == home:
        return "${HOME}"
    prefix = home + os.sep
    if normalized.startswith(prefix):
        relative = normalized[len(prefix):].replace(os.sep, "/")
        return "${HOME}/" + relative
    return normalized


_CONTENT_ASSIGNMENT_RE = re.compile(
    r"(?im)^\s*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|credential|authorization)\s*[:=]\s*(.+?)\s*$"
)
_PRIVATE_MARKERS = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN PGP PRIVATE KEY BLOCK-----",
)
_SAFE_PLACEHOLDERS = {
    "", "none", "null", "false", "true", "0", "ask", "prompt", "changeme",
    "<redacted>", "redacted", "${token}", "${password}",
}


def scan_object_contents(
    entries,
    read_object: Callable[[str], bytes],
    max_bytes: int = 512 * 1024,
) -> list[Finding]:
    """Inspección ligera de texto para secretos evidentes.

    No intenta interpretar todos los formatos ni promete detectar cualquier
    secreto. Los binarios y archivos grandes se omiten para mantener la captura
    predecible.
    """
    findings: list[Finding] = []
    for entry in entries:
        size = int(getattr(entry, "size", 0) or 0)
        checksum = str(getattr(entry, "checksum", "") or "")
        if not checksum or size > max_bytes:
            continue
        try:
            data = read_object(checksum)
        except Exception:  # la integridad se reporta por otra validación
            continue
        if len(data) > max_bytes or b"\x00" in data:
            continue
        text = data.decode("utf-8", "ignore")
        if any(marker in text for marker in _PRIVATE_MARKERS):
            findings.append(Finding.suspicious_content(str(entry.path)))
            continue
        for match in _CONTENT_ASSIGNMENT_RE.finditer(text):
            value = match.group(1).strip().strip('"\'').lower()
            if value in _SAFE_PLACEHOLDERS or value.startswith("${") or value.startswith("<"):
                continue
            if len(value) >= 4:
                findings.append(Finding.suspicious_content(str(entry.path)))
                break
    return findings
