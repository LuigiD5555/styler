"""Resolución de rutas del catálogo: ``${HOME}`` y el esquema ``catalog://``.

Dos traducciones que el resto del paquete necesita y que deben estar en un
solo sitio, porque ambas son superficie de seguridad:

- ``${HOME}/.config/GIMP`` → ruta real del usuario. Se rechaza cualquier
  ruta que, tras expandir, escape del HOME (un ``..`` en un TOML del nivel
  de usuario no debe poder apuntar a ``/etc``).
- ``catalog://photogimp`` → el asset empaquetado en
  ``styler/catalog/components/assets/photogimp``. Se rechaza cualquier
  nombre que escape del directorio de assets.

Ninguna de las dos inventa rutas: si no puede resolver, devuelve ``None`` y
quien llame decide (los ejecutores fallan explícito).
"""
from __future__ import annotations

import os
from pathlib import Path

CATALOG_SCHEME = "catalog://"

ASSETS_ROOT = Path(__file__).resolve().parent.parent / "catalog" / "components" / "assets"


class PathResolutionError(Exception):
    """Una ruta del catálogo no pudo resolverse de forma segura."""


def expand_user_path(raw: str, home: Path | None = None) -> Path:
    """Expande ``${HOME}`` y ``~`` y comprueba que no escape del HOME.

    Solo se permiten rutas dentro del HOME: los componentes de este catálogo
    describen configuración *de usuario*. Escribir en ``/etc`` es trabajo de
    un proveedor de paquetes con privilegios, no de un overlay de usuario.
    """
    if not raw:
        raise PathResolutionError("ruta vacía")
    home = (home or Path.home()).resolve()
    expanded = raw.replace("${HOME}", str(home)).replace("$HOME", str(home))
    candidate = Path(os.path.expanduser(expanded))
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise PathResolutionError(f"no se pudo resolver '{raw}': {exc}") from exc
    if resolved != home and home not in resolved.parents:
        raise PathResolutionError(
            f"'{raw}' apunta fuera del HOME del usuario ({resolved}); no se permite."
        )
    return resolved


def resolve_catalog_uri(uri: str, assets_root: Path | None = None) -> Path:
    """``catalog://<nombre>`` → directorio del asset empaquetado.

    Lanza ``PathResolutionError`` si el URI no es del esquema esperado, si el
    nombre escapa del directorio de assets, o si el asset no existe. No se
    devuelve una ruta que no exista: un ejecutor no debe intentar extraer de
    un directorio inventado.
    """
    if not uri.startswith(CATALOG_SCHEME):
        raise PathResolutionError(f"'{uri}' no usa el esquema {CATALOG_SCHEME}")
    name = uri[len(CATALOG_SCHEME):].strip("/")
    if not name:
        raise PathResolutionError(f"'{uri}' no nombra ningún asset")

    root = (assets_root or ASSETS_ROOT).resolve()
    candidate = (root / name).resolve()
    if root not in candidate.parents and candidate != root:
        raise PathResolutionError(f"'{uri}' escapa del directorio de assets del catálogo")
    if not candidate.exists():
        raise PathResolutionError(
            f"el asset '{name}' no existe en el catálogo ({candidate}). "
            "Agrégalo bajo styler/catalog/components/assets/ antes de usarlo."
        )
    return candidate
