"""Traduce errores de dominio a mensajes comprensibles sin mostrar tracebacks."""
from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass

from styler.objectstore import ObjectStoreError
from styler.portable.models import PortablePackageError
from styler.services import (
    AuthorizationError,
    OperationCancelledError,
    UserError,
)
from styler.validation import ValidationError

logger = logging.getLogger("styler.ui")


@dataclass(frozen=True)
class UserFacingError:
    title: str
    message: str
    recovery: str | None
    technical_code: str
    technical_detail: str

    def clipboard_text(self) -> str:
        return f"[{self.technical_code}] {self.title}\n{self.message}\n\n{self.technical_detail}"


_MAPPINGS: tuple[tuple[type[Exception], str, str, str, str | None], ...] = (
    (OperationCancelledError, "CANCELLED", "La operación fue cancelada de forma segura", "", None),
    (AuthorizationError, "AUTHORIZATION", "No se pudo obtener autorización administrativa", "", "Vuelve a intentarlo y escribe la contraseña en el terminal cuando se solicite."),
    (PortablePackageError, "PORTABLE_PACKAGE", "No se pudo abrir este paquete", "El archivo está dañado, es incompatible o contiene una declaración insegura.", "Revisa los detalles y crea o solicita un paquete corregido."),
    (ObjectStoreError, "OBJECT", "Falta contenido en la biblioteca", "Uno de los archivos está dañado o incompleto.", "Vuelve a crear o importar el paquete completo."),
    (ValidationError, "VALIDATION", "Hay un dato que Styler no acepta", "Un identificador o una ruta no cumple las reglas de seguridad.", None),
    (UserError, "USER", "No se puede continuar todavía", "", None),
)


def to_user_error(exc: Exception) -> UserFacingError:
    detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    logger.debug("Error de dominio", exc_info=exc)
    for kind, code, title, message, recovery in _MAPPINGS:
        if isinstance(exc, kind):
            body = message or str(exc)
            technical = getattr(exc, "technical_detail", "") or getattr(exc, "detail", "") or detail
            return UserFacingError(title, body, recovery, code, technical)
    return UserFacingError(
        "Ocurrió un problema inesperado",
        "Styler detuvo la operación para no dejar tu equipo a medias.",
        "Puedes copiar los detalles técnicos y reportarlos.",
        "UNEXPECTED",
        detail,
    )
