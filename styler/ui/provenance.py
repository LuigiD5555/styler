"""
styler.ui.provenance
====================
Servicio de procedencia para la interfaz. La TUI habla SOLO con esta capa:
nunca ejecuta `dpkg-query`, nunca parsea la salida de la CLI.

Regla de la pantalla: una persona no técnica debe poder responder
«¿de dónde salió esta aplicación y podría recuperarla?» sin abrir una terminal.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from styler.provenance import inventory as inventory_mod
from styler.provenance import report as report_mod
from styler.provenance import baseline as baseline_mod
from styler.provenance import vault as vault_mod
from styler.provenance.models import ApplicationRecord, BaselineRole, Confidence, Inventory
from styler import advanced_restore
from styler.services import UserError

ProgressHook = Optional[Callable[[str, int, int], None]]

_ORIGIN_HUMAN = {
    "apt": "Repositorio del sistema (APT)",
    "flatpak": "Flatpak",
    "snap": "Snap",
    "pacman": "Repositorio del sistema (pacman)",
    "rpm": "Repositorio del sistema (RPM)",
    "appimage": "Archivo AppImage",
    "manual": "Instalación manual",
    "unknown": "Origen desconocido",
}


@dataclass(frozen=True)
class ApplicationView:
    app_id: str
    name: str
    version: str
    manager: str
    origin_label: str
    origin_detail: str
    confidence: str
    confidence_level: str
    recoverable: bool
    upstream: str
    install_reason: str = "unknown"
    baseline_role: str = "unknown"
    warnings: tuple[str, ...] = ()

    @property
    def status_line(self) -> str:
        return "Se puede recuperar" if self.recoverable else "Sin forma comprobada de recuperarla"


@dataclass(frozen=True)
class AdvancedRestoreSettingsView:
    enabled: bool
    repository_lookup: bool
    alternative_versions: bool
    provider_change: bool
    installation: bool

    @property
    def label(self) -> str:
        return "activada" if self.enabled else "desactivada"


@dataclass(frozen=True)
class RestoreCandidateView:
    candidate_id: str
    manager: str
    name: str
    version: str
    source_type: str
    source: str
    relation: str
    same_provider: bool
    installable: bool
    notes: tuple[str, ...] = ()

    @property
    def alternative(self) -> bool:
        return self.relation in {"older", "newer", "unknown"}


@dataclass(frozen=True)
class CandidateSearchView:
    app_id: str
    requested_name: str
    desired_version: str
    candidates: tuple[RestoreCandidateView, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RestoreActionView:
    ok: bool
    message: str
    command: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class InventoryView:
    inventory_id: str
    captured_at: float
    distro: str
    scope: str
    applications: tuple[ApplicationView, ...] = ()
    managers: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def when(self) -> str:
        return time.strftime("%d/%m/%Y %H:%M", time.localtime(self.captured_at))

    @property
    def total(self) -> int:
        return len(self.applications)

    @property
    def at_risk(self) -> tuple[ApplicationView, ...]:
        return tuple(app for app in self.applications if not app.recoverable)


def _origin_detail(record: ApplicationRecord) -> str:
    origin = record.origin
    pieces = [piece for piece in (origin.remote_name, origin.branch) if piece]
    detail = " · ".join(dict.fromkeys(pieces))
    if origin.remote_url:
        detail = f"{detail} ({origin.remote_url})" if detail else origin.remote_url
    return detail or "No se pudo determinar"


def _upstream_label(record: ApplicationRecord) -> str:
    upstream = record.upstream
    if upstream.repository:
        return f"{upstream.repository} — {upstream.confidence.human.lower()}"
    if upstream.packaging_repository:
        return f"empaquetado en {upstream.packaging_repository}"
    if upstream.homepage:
        return f"sitio del proyecto: {upstream.homepage}"
    return "Desconocido"


def to_view(record: ApplicationRecord) -> ApplicationView:
    return ApplicationView(
        app_id=record.app_id,
        name=record.display_name or record.name,
        version=record.version,
        manager=record.manager,
        origin_label=_ORIGIN_HUMAN.get(record.origin.kind.value, "Origen desconocido"),
        origin_detail=_origin_detail(record),
        confidence=record.origin.confidence.human,
        confidence_level=record.origin.confidence.value,
        recoverable=record.reproducible_today,
        upstream=_upstream_label(record),
        install_reason=record.install_reason.value,
        baseline_role=record.baseline_role.value,
        warnings=tuple(record.warnings),
    )


def to_inventory_view(inventory: Inventory, problems: list[str] | None = None) -> InventoryView:
    return InventoryView(
        inventory_id=inventory.inventory_id,
        captured_at=inventory.captured_at,
        distro=inventory.distro,
        scope=inventory.scope,
        applications=tuple(to_view(record) for record in inventory.applications),
        managers=tuple(inventory.managers_seen),
        problems=tuple(problems or []),
    )


@dataclass
class ProvenanceService:
    """Escanea, guarda, consulta y exporta la procedencia. Nunca instala nada."""

    root: str = "."
    _problems: list[str] = field(default_factory=list)

    def scan(self, scope: str = "apps", progress: ProgressHook = None) -> InventoryView:
        try:
            inventory, problems = inventory_mod.scan(scope=scope, progress=progress)
        except inventory_mod.ProvenanceError as exc:
            raise UserError(str(exc)) from exc
        inventory_mod.save_inventory(inventory, root=self.root)
        self._problems = problems
        return to_inventory_view(inventory, problems)

    def latest(self) -> Optional[InventoryView]:
        inventory = inventory_mod.latest_inventory(root=self.root)
        if inventory is None:
            return None
        return to_inventory_view(inventory, self._problems)

    def detail(self, app_id: str) -> str:
        inventory = self._require_latest()
        record = inventory.by_id(app_id)
        if record is None:
            matches = inventory.find(app_id)
            if not matches:
                raise UserError(f"No hay ninguna aplicación registrada como «{app_id}».")
            record = matches[0]
        return report_mod.detail(record)

    def report(self) -> str:
        return report_mod.full_report(self._require_latest())

    def set_baseline(self) -> str:
        inventory = self._require_latest()
        for record in inventory.applications:
            record.baseline_role = BaselineRole.BASE
        inventory_mod.save_inventory(inventory, root=self.root)
        return baseline_mod.save_baseline(inventory, root=self.root)

    def baseline_report(self) -> str:
        baseline = baseline_mod.load_baseline(root=self.root)
        if baseline is None:
            raise UserError("Todavía no has marcado una línea base.")
        current = self._require_latest()
        comparison = baseline_mod.compare(baseline, current)
        inventory_mod.save_inventory(current, root=self.root)
        return baseline_mod.summary(comparison)

    def preserve_artifacts(self, added_only: bool = False) -> tuple[int, int, tuple[str, ...]]:
        inventory = self._require_latest()
        app_ids: set[str] | None = None
        if added_only:
            baseline = baseline_mod.load_baseline(root=self.root)
            if baseline is None:
                raise UserError("Marca una línea base antes de conservar solo lo añadido.")
            comparison = baseline_mod.compare(baseline, inventory)
            app_ids = {record.app_id for record in comparison.added}
        results = vault_mod.preserve_inventory_artifacts(
            inventory,
            root=self.root,
            app_ids=app_ids,
        )
        inventory_mod.save_inventory(inventory, root=self.root)
        preserved = sum(
            result.status in {"preserved", "already-present"} for result in results
        )
        messages = tuple(
            f"{result.app_id}: {result.message}"
            for result in results
            if result.status in {"failed", "unavailable"} and result.message
        )
        return preserved, len(results), messages

    def restore_candidates(self, app_id: str) -> CandidateSearchView:
        inventory = self._require_latest()
        record = inventory.by_id(app_id)
        if record is None:
            raise UserError(f"No hay ninguna aplicación registrada como «{app_id}».")
        settings = advanced_restore.load_settings(self.root)
        try:
            from styler.component_graph import capabilities_for_package
            from styler.models import Package

            capabilities = capabilities_for_package(
                Package(record.manager, record.name, record.version, record.architecture)
            )
            capability = next(
                (item for item in capabilities if item.startswith(("application.", "desktop."))),
                "",
            )
            result = advanced_restore.candidates_for_application(
                record,
                settings,
                capability=capability,
                root=self.root,
            )
        except advanced_restore.AdvancedRestoreError as exc:
            raise UserError(str(exc)) from exc
        return CandidateSearchView(
            app_id=record.app_id,
            requested_name=result.requested_name,
            desired_version=result.desired_version,
            candidates=tuple(
                RestoreCandidateView(
                    candidate_id=item.candidate_id,
                    manager=item.manager,
                    name=item.name,
                    version=item.version,
                    source_type=item.source_type,
                    source=item.source,
                    relation=item.relation,
                    same_provider=item.same_provider,
                    installable=item.installable,
                    notes=item.notes,
                )
                for item in result.candidates
            ),
            warnings=tuple(result.warnings),
        )

    def restore_candidate(
        self,
        app_id: str,
        candidate_id: str,
        *,
        approve_alternative_version: bool,
        approve_provider_change: bool,
    ) -> RestoreActionView:
        inventory = self._require_latest()
        record = inventory.by_id(app_id)
        if record is None:
            raise UserError(f"No hay ninguna aplicación registrada como «{app_id}».")
        settings = advanced_restore.load_settings(self.root)
        try:
            from styler.component_graph import capabilities_for_package
            from styler.models import Package

            capabilities = capabilities_for_package(
                Package(record.manager, record.name, record.version, record.architecture)
            )
            capability = next(
                (item for item in capabilities if item.startswith(("application.", "desktop."))),
                "",
            )
            search = advanced_restore.candidates_for_application(
                record,
                settings,
                capability=capability,
                root=self.root,
            )
            candidate = next(
                (item for item in search.candidates if item.candidate_id == candidate_id),
                None,
            )
            if candidate is None:
                raise advanced_restore.AdvancedRestoreError(
                    "La opción elegida ya no está disponible. Vuelve a buscar antes de instalar."
                )
            result = advanced_restore.install_candidate(
                candidate,
                settings,
                execute=True,
                approve=True,
                approve_alternative_version=approve_alternative_version,
                approve_provider_change=approve_provider_change,
            )
        except advanced_restore.AdvancedRestoreError as exc:
            raise UserError(str(exc)) from exc
        return RestoreActionView(
            ok=result.success,
            message=result.message,
            command=result.command,
            detail=result.stderr or result.stdout,
        )

    def advanced_restore_settings(self) -> AdvancedRestoreSettingsView:
        settings = advanced_restore.load_settings(self.root)
        return AdvancedRestoreSettingsView(
            enabled=settings.enabled,
            repository_lookup=settings.allow_repository_lookup,
            alternative_versions=settings.allow_alternative_versions,
            provider_change=settings.allow_provider_change,
            installation=settings.allow_installation,
        )

    def configure_advanced_restore(
        self,
        *,
        enabled: bool,
        repository_lookup: bool,
        alternative_versions: bool,
        provider_change: bool,
        installation: bool,
        acknowledge_risk: bool,
    ) -> AdvancedRestoreSettingsView:
        try:
            advanced_restore.configure_settings(
                self.root,
                enabled=enabled,
                allow_repository_lookup=repository_lookup,
                allow_alternative_versions=alternative_versions,
                allow_provider_change=provider_change,
                allow_installation=installation,
                acknowledge_risk=acknowledge_risk,
            )
        except advanced_restore.AdvancedRestoreError as exc:
            raise UserError(str(exc)) from exc
        return self.advanced_restore_settings()

    def export(self, destination: str | Path) -> str:
        inventory = self._require_latest()
        path = Path(destination).expanduser()
        payload = json.dumps(inventory.to_dict(), indent=2, ensure_ascii=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        return str(path)

    def _require_latest(self) -> Inventory:
        inventory = inventory_mod.latest_inventory(root=self.root)
        if inventory is None:
            raise UserError(
                "Todavía no hay un catálogo de procedencia. Ejecuta un análisis primero."
            )
        return inventory


__all__ = [
    "AdvancedRestoreSettingsView",
    "CandidateSearchView",
    "RestoreActionView",
    "RestoreCandidateView",
    "ApplicationView",
    "Confidence",
    "InventoryView",
    "ProvenanceService",
    "to_inventory_view",
    "to_view",
]
