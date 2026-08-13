"""
styler.provenance
=================
Styler 0.8.2 — Procedencia de aplicaciones (solo lectura).

Responde, para cada aplicación instalada:

    ¿qué es · quién la instaló · desde dónde · qué versión ·
    qué archivo la recrea · qué tan confiable es esa información?

No instala ni publica nada. Puede conservar copias verificadas de artefactos
que ya existen, pero no descarga los faltantes.
"""
from styler.provenance.inventory import (
    ProvenanceError,
    latest_inventory,
    list_inventories,
    load_inventory,
    save_inventory,
    scan,
)
from styler.provenance.models import (
    ApplicationRecord,
    BaselineRole,
    Confidence,
    InstallReason,
    Integrity,
    Inventory,
    Origin,
    OriginKind,
    SystemIdentity,
    Upstream,
)

__all__ = [
    "ApplicationRecord",
    "BaselineRole",
    "Confidence",
    "InstallReason",
    "Integrity",
    "Inventory",
    "Origin",
    "OriginKind",
    "SystemIdentity",
    "ProvenanceError",
    "Upstream",
    "latest_inventory",
    "list_inventories",
    "load_inventory",
    "save_inventory",
    "scan",
]
