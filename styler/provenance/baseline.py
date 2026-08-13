"""Comparación de aplicaciones y recursos visuales contra una línea base."""
from __future__ import annotations

from dataclasses import dataclass, field

from styler.provenance.models import (
    ApplicationRecord, BaselineRole, Inventory, SystemArtifactRecord,
)


@dataclass
class BaselineComparison:
    baseline_id: str
    current_id: str
    added: list[ApplicationRecord] = field(default_factory=list)
    removed: list[ApplicationRecord] = field(default_factory=list)
    changed: list[tuple[ApplicationRecord, ApplicationRecord]] = field(default_factory=list)
    unchanged: list[ApplicationRecord] = field(default_factory=list)
    artifacts_added: list[SystemArtifactRecord] = field(default_factory=list)
    artifacts_removed: list[SystemArtifactRecord] = field(default_factory=list)
    artifacts_changed: list[tuple[SystemArtifactRecord, SystemArtifactRecord]] = field(default_factory=list)
    artifacts_unchanged: list[SystemArtifactRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def comparable_system(self) -> bool:
        return not self.warnings

    def to_dict(self) -> dict:
        return {
            "baseline_id": self.baseline_id, "current_id": self.current_id,
            "added": [x.to_dict() for x in self.added],
            "removed": [x.to_dict() for x in self.removed],
            "changed": [{"before": a.to_dict(), "after": b.to_dict()} for a, b in self.changed],
            "unchanged": [x.to_dict() for x in self.unchanged],
            "artifacts_added": [x.to_dict() for x in self.artifacts_added],
            "artifacts_removed": [x.to_dict() for x in self.artifacts_removed],
            "artifacts_changed": [{"before": a.to_dict(), "after": b.to_dict()} for a, b in self.artifacts_changed],
            "artifacts_unchanged": [x.to_dict() for x in self.artifacts_unchanged],
            "warnings": list(self.warnings), "comparable_system": self.comparable_system,
        }


def compare(baseline: Inventory, current: Inventory) -> BaselineComparison:
    old = {record.app_id: record for record in baseline.applications}
    new = {record.app_id: record for record in current.applications}
    result = BaselineComparison(baseline.inventory_id, current.inventory_id)
    old_system, new_system = baseline.system.comparable_key(), current.system.comparable_key()
    if any(old_system) and any(new_system) and old_system != new_system:
        result.warnings.append(
            "La línea base y el inventario actual no pertenecen a la misma distribución, versión, variante o arquitectura."
        )
    for app_id in sorted(new):
        record, previous = new[app_id], old.get(app_id)
        if previous is None:
            record.baseline_role = BaselineRole.ADDED; result.added.append(record)
        elif _app_identity(previous) != _app_identity(record):
            record.baseline_role = BaselineRole.CHANGED; result.changed.append((previous, record))
        else:
            record.baseline_role = BaselineRole.BASE; result.unchanged.append(record)
    for app_id in sorted(set(old) - set(new)):
        record = old[app_id]; record.baseline_role = BaselineRole.REMOVED; result.removed.append(record)

    old_art = {item.artifact_id: item for item in baseline.artifacts}
    new_art = {item.artifact_id: item for item in current.artifacts}
    for artifact_id in sorted(new_art):
        item, previous = new_art[artifact_id], old_art.get(artifact_id)
        if previous is None:
            result.artifacts_added.append(item)
        elif _artifact_identity(previous) != _artifact_identity(item):
            result.artifacts_changed.append((previous, item))
        else:
            result.artifacts_unchanged.append(item)
    for artifact_id in sorted(set(old_art) - set(new_art)):
        result.artifacts_removed.append(old_art[artifact_id])
    return result


def summary(comparison: BaselineComparison) -> str:
    return "\n".join([
        f"Comparación contra línea base {comparison.baseline_id}",
        f"Aplicaciones añadidas: {len(comparison.added)}",
        f"Aplicaciones cambiadas: {len(comparison.changed)}",
        f"Recursos visuales añadidos: {len(comparison.artifacts_added)}",
        f"Recursos visuales cambiados: {len(comparison.artifacts_changed)}",
    ])


def _app_identity(record: ApplicationRecord) -> tuple:
    return (record.version, record.architecture, record.manager, record.install_method,
            record.origin.remote_name, record.origin.remote_url, record.origin.branch, record.origin.commit)


def _artifact_identity(record: SystemArtifactRecord) -> tuple:
    return (record.kind.value, record.path, record.checksum, record.mode, record.size, record.file_count)
