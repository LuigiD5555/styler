"""Validaciones cerradas para identificadores, checksums y rutas portables.

Este módulo es una frontera de seguridad: cualquier dato proveniente de un
bundle, JSON local o argumento de servicio debe validarse antes de convertirse
en una ruta del sistema de archivos.
"""
from __future__ import annotations

import re
from pathlib import Path

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CHECKSUM_RE = re.compile(r"^[0-9a-f]{32}$")
HOME_TOKEN = "${HOME}"


class ValidationError(ValueError):
    """Dato inválido que no debe cruzar una frontera persistente."""


def validate_identifier(value: str, field: str = "identificador") -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise ValidationError(
            f"{field} inválido: usa letras, números, punto, guion o guion bajo "
            "(máximo 128 caracteres)."
        )
    return value


def validate_checksum(value: str) -> str:
    if not isinstance(value, str) or not CHECKSUM_RE.fullmatch(value):
        raise ValidationError("Checksum inválido: se esperaban 32 caracteres hexadecimales.")
    return value


def validate_logical_path(value: str) -> str:
    """Acepta exclusivamente ${HOME} o ${HOME}/ruta-relativa segura.

    Las rutas del sistema quedan fuera del contrato portable de esta versión.
    Se rechazan barras invertidas para no tener interpretaciones distintas
    entre plataformas y cualquier componente '.' o '..'.
    """
    if not isinstance(value, str) or not value:
        raise ValidationError("Ruta portable vacía o inválida.")
    if "\x00" in value or "\\" in value:
        raise ValidationError(f"Ruta portable inválida: {value!r}")
    if value == HOME_TOKEN:
        return value
    prefix = HOME_TOKEN + "/"
    if not value.startswith(prefix):
        raise ValidationError(
            f"Ruta fuera del área administrada: {value}. "
            "Esta versión solo admite archivos dentro de ${HOME}."
        )
    relative = value[len(prefix):]
    if not relative or relative.startswith("/"):
        raise ValidationError(f"Ruta portable inválida: {value}")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValidationError(f"Ruta portable insegura: {value}")
    return value


def resolve_home_path(logical_path: str, home: str | Path) -> Path:
    """Convierte una ruta portable en destino real y confirma confinamiento.

    ``resolve(strict=False)`` sigue enlaces existentes en los padres; si alguno
    apunta fuera de HOME, la comprobación ``relative_to`` lo detecta.
    """
    validate_logical_path(logical_path)
    home_path = Path(home).expanduser().resolve()
    if logical_path == HOME_TOKEN:
        candidate = home_path
    else:
        candidate = home_path / logical_path[len(HOME_TOKEN + "/"):]
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(home_path)
    except ValueError as exc:
        raise ValidationError(f"La ruta escapa del directorio personal: {logical_path}") from exc
    return resolved


def safe_record_path(base: str | Path, identifier: str, suffix: str = ".json") -> Path:
    validate_identifier(identifier)
    base_path = Path(base).resolve()
    candidate = (base_path / f"{identifier}{suffix}").resolve(strict=False)
    try:
        candidate.relative_to(base_path)
    except ValueError as exc:
        raise ValidationError("El identificador intentó salir de la biblioteca.") from exc
    return candidate
