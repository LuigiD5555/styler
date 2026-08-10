"""Catálogo declarativo canónico de componentes de Styler.

El catálogo TOML es la única fuente de verdad para capacidades, proveedores,
dependencias, verificación, rollback y compilación al contrato de ejecución
compatible con PipeCraft 1.3.1.
"""
from styler.component_catalog.compiler import CompileResult, compile_workflow
from styler.component_catalog.errors import ComponentCatalogError
from styler.component_catalog.loader import LoadReport, load
from styler.component_catalog.models import ComponentDefinition
from styler.component_catalog.registry import ComponentRegistry
from styler.component_catalog.resolver import ResolutionResult, resolve
from styler.component_catalog.validator import ValidationReport, validate

__all__ = [
    "CompileResult",
    "ComponentCatalogError",
    "ComponentDefinition",
    "ComponentRegistry",
    "LoadReport",
    "ResolutionResult",
    "ValidationReport",
    "compile_workflow",
    "load",
    "resolve",
    "validate",
]
