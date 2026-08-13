"""Errores del catálogo declarativo de componentes.

Estos errores son de *carga* (TOML mal formado, ruta insegura, índice
inconsistente). Los problemas de contenido semántico (capacidad sin
proveedor, ciclo, rollback irreal) no son excepciones: son
``ValidationIssue`` que produce ``validator.py``, porque un catálogo con
advertencias debe poder inspeccionarse sin abortar el programa.
"""
from __future__ import annotations


class ComponentCatalogError(Exception):
    """Raíz de todos los errores del catálogo de componentes."""


class CatalogPathError(ComponentCatalogError):
    """Una ruta del índice escapa de la raíz del catálogo o no existe."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


class CatalogParseError(ComponentCatalogError):
    """Un archivo TOML no pudo interpretarse."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: TOML inválido ({reason})")


class CatalogIndexError(ComponentCatalogError):
    """El índice central (``index.toml``) tiene una entrada inconsistente."""

    def __init__(self, component_id: str, reason: str) -> None:
        self.component_id = component_id
        self.reason = reason
        super().__init__(f"{component_id}: {reason}")


class DuplicateComponentError(ComponentCatalogError):
    """Dos definiciones del mismo nivel declaran el mismo ID."""

    def __init__(self, component_id: str, first_source: str, second_source: str) -> None:
        self.component_id = component_id
        self.first_source = first_source
        self.second_source = second_source
        super().__init__(
            f"{component_id} está definido dos veces en el mismo nivel "
            f"({first_source} y {second_source})"
        )


class UnsupportedSchemaVersionError(ComponentCatalogError):
    """``schema_version`` no es soportado por esta versión de Styler."""

    def __init__(self, path: str, found: int, supported: tuple[int, ...]) -> None:
        self.path = path
        self.found = found
        self.supported = supported
        super().__init__(
            f"{path}: schema_version {found} no soportado (soportadas: {supported})"
        )
