"""Formato legible de reportes del catálogo, para CLI y TUI.

No decide qué mostrar en pantalla (eso es de ``styler/cli.py`` y
``styler/tui``); solo convierte ``LoadReport``/``ValidationReport`` en
texto plano estable, para no duplicar el formato en cada punto de uso.
"""
from __future__ import annotations

from styler.component_catalog.loader import LoadReport
from styler.component_catalog.validator import ValidationReport


def format_load_report(report: LoadReport) -> str:
    lines: list[str] = []
    lines.append(f"Componentes cargados: {len(report.components)}")
    for component_id in sorted(report.components):
        loaded = report.components[component_id]
        lines.append(f"  {component_id}  [{loaded.source.level}]  {loaded.source.path}")
    if report.overrides:
        lines.append("Sobreescrituras:")
        lines.extend(f"  - {message}" for message in report.overrides)
    if report.orphan_files:
        lines.append("Archivos huérfanos (no registrados en su índice):")
        lines.extend(f"  - {path}" for path in report.orphan_files)
    if report.warnings:
        lines.append("Advertencias:")
        lines.extend(f"  - {message}" for message in report.warnings)
    return "\n".join(lines)


def format_validation_report(report: ValidationReport) -> str:
    if not report.issues:
        return "Catálogo válido: sin observaciones."
    lines: list[str] = []
    for issue in report.issues:
        marker = "ERROR" if issue.severity == "error" else "AVISO"
        lines.append(f"[{marker}] {issue.code} {issue.component_id} ({issue.field}): {issue.message}")
        if issue.suggestion:
            lines.append(f"        sugerencia: {issue.suggestion}")
    errors = len(report.errors)
    warnings = len(report.warnings)
    lines.append(f"Total: {errors} error(es), {warnings} aviso(s).")
    return "\n".join(lines)
