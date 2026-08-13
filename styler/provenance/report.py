"""
styler.provenance.report
========================
Presentación en texto del catálogo de procedencia: «Origen de las aplicaciones».

El reporte nunca afirma más de lo que sabe. Cada línea trae su nivel de
confianza y las aplicaciones que hoy no se podrían reconstruir aparecen
separadas, porque son las que importan cuando algo se rompe.
"""
from __future__ import annotations

import time

from styler.provenance.models import ApplicationRecord, Confidence, Inventory

BADGE = {
    Confidence.CONFIRMED: "✓ confirmado",
    Confidence.INFERRED: "· inferido",
    Confidence.SUGGESTED: "? sugerido",
    Confidence.UNKNOWN: "! desconocido",
}


def _when(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


def summary(inventory: Inventory) -> str:
    counts = inventory.counts_by_manager()
    confidence = inventory.counts_by_confidence()
    attention = inventory.needs_attention()

    lines = [
        f"Origen de las aplicaciones — {inventory.distro or 'este equipo'}",
        f"Inventario {inventory.inventory_id} · {_when(inventory.captured_at)} · "
        f"alcance: {inventory.scope}",
        (
            "Sistema base: "
            + " ".join(
                value for value in (
                    inventory.system.distro_id,
                    inventory.system.distro_version,
                    inventory.system.distro_variant,
                    inventory.system.architecture,
                ) if value
            )
        ),
        "",
        f"Aplicaciones registradas: {len(inventory.applications)}",
    ]
    if counts:
        detail = ", ".join(f"{manager}: {count}" for manager, count in counts.items())
        lines.append(f"Por gestor: {detail}")
    lines.append(
        "Confianza del origen: "
        f"confirmado {confidence['confirmed']}, "
        f"inferido {confidence['inferred']}, "
        f"sugerido {confidence['suggested']}, "
        f"desconocido {confidence['unknown']}"
    )
    reasons: dict[str, int] = {}
    for record in inventory.applications:
        reasons[record.install_reason.value] = reasons.get(record.install_reason.value, 0) + 1
    if reasons:
        lines.append(
            "Razón de instalación: "
            + ", ".join(f"{reason}: {count}" for reason, count in sorted(reasons.items()))
        )
    lines.append(
        f"Sin artefacto ni origen confirmado para recuperarlas: {len(attention)}"
    )
    return "\n".join(lines)


def table(inventory: Inventory) -> str:
    if not inventory.applications:
        return "No se encontraron aplicaciones."

    rows = [("APLICACIÓN", "VERSIÓN", "GESTOR", "ORIGEN", "CONFIANZA")]
    for record in inventory.applications:
        rows.append(
            (
                record.name[:34],
                record.version[:18],
                record.manager,
                (record.origin.remote_name or "—")[:30],
                BADGE[record.origin.confidence],
            )
        )

    widths = [max(len(row[column]) for row in rows) for column in range(5)]
    lines = []
    for index, row in enumerate(rows):
        lines.append("  ".join(value.ljust(widths[column]) for column, value in enumerate(row)).rstrip())
        if index == 0:
            lines.append("  ".join("─" * width for width in widths))
    return "\n".join(lines)


def attention(inventory: Inventory) -> str:
    pending = inventory.needs_attention()
    if not pending:
        return "Todas las aplicaciones tienen un origen comprobable."

    lines = ["Necesitan atención (no hay remote confirmado ni archivo guardado):", ""]
    for record in pending:
        lines.append(f"  {record.app_id}  {record.version}")
        for warning in record.warnings or ["Origen desconocido."]:
            lines.append(f"      · {warning}")
    return "\n".join(lines)


def detail(record: ApplicationRecord) -> str:
    origin = record.origin
    upstream = record.upstream
    integrity = record.integrity

    lines = [
        f"{record.display_name or record.name}  ({record.app_id})",
        "",
        f"  Instalada:        {record.version or '—'} {record.architecture}".rstrip(),
        f"  Gestor:           {record.manager}",
        f"  Método:           {record.install_method or '—'}",
        f"  Razón:            {record.install_reason.value}",
        f"  Relación con base:{(' ' + record.baseline_role.value) if record.baseline_role.value else ' —'}",
        "",
        "  Origen de la aplicación",
        f"    Tipo:           {origin.kind.value}",
        f"    Remote:         {origin.remote_name or '—'}",
        f"    URL:            {origin.remote_url or '—'}",
        f"    Rama/canal:     {origin.branch or origin.channel or '—'}",
        f"    Referencia:     {origin.ref or '—'}",
        f"    Commit/revisión:{(' ' + origin.commit) if origin.commit else ' —'}",
        f"    Proveedor:      {origin.vendor or '—'}",
        f"    Firmado:        {_tri(origin.signed)}",
        f"    Confianza:      {origin.confidence.human}",
        f"    Evidencia:      {origin.evidence or '—'}",
        "",
        "  Repositorio upstream",
        f"    Proveedor:      {upstream.provider or '—'}",
        f"    Repositorio:    {upstream.repository or '—'}",
        f"    URL:            {upstream.url or upstream.homepage or '—'}",
        f"    Empaquetado en: {upstream.packaging_repository or '—'}",
        f"    Confianza:      {upstream.confidence.human}",
        "",
        "  Integridad",
        f"    Checksum:       {integrity.checksum or '—'}",
        f"    Firma:          {_tri(integrity.signature_verified)}",
        f"    Artefacto:      {integrity.artifact_path or '—'}",
        f"    Reinstalable hoy sin red: {'sí' if integrity.artifact_available else 'no'}",
        "",
        f"  Recuperable con la evidencia actual: {'sí' if record.reproducible_today else 'no'}",
    ]
    if record.warnings:
        lines.append("")
        lines.append("  Avisos")
        for warning in record.warnings:
            lines.append(f"    · {warning}")
    return "\n".join(lines)


def _tri(value: bool | None) -> str:
    if value is None:
        return "no se pudo determinar"
    return "sí" if value else "no"


def full_report(inventory: Inventory) -> str:
    return "\n\n".join([summary(inventory), table(inventory), attention(inventory)])
