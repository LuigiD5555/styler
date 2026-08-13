"""Constructor unificado: línea base, detección, selección, receta, DAG y paquete."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Sequence

from styler.baselines import (
    BaselineError, BaselineKind, BaselineService, CompatibilityScope, CompatibilityStatus,
)
from styler.change_recipe import (
    SynthesisResult, compile_recipe, dumps_recipe, synthesize_recipe,
)
from styler.portable import (
    ArtifactEntry, GraphDefinition, PackageManifest, PackageType, PortableLibrary,
    build_package, normalize_identifier,
)
from styler.provenance import baseline as baseline_mod
from styler.provenance import inventory as inventory_mod
from styler.provenance.classification import can_generate_install, classify, is_user_choice
from styler.provenance.models import ApplicationRecord, Inventory, SystemArtifactRecord

CONSTRUCTOR_PACKAGE_VERSION = "1.0.0"

class ConstructorError(Exception):
    pass


@dataclass(frozen=True)
class DetectedChangeView:
    change_id: str
    name: str
    manager: str
    version: str
    category: str
    category_label: str
    role: str
    confidence: str
    exportable: bool
    needs_opt_in: bool
    reason: str
    source_kind: str = "application"
    source_path: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def status_line(self) -> str:
        parts = [self.category_label]
        if self.manager:
            parts.append(self.manager)
        if self.version:
            parts.append(self.version)
        if not self.exportable:
            parts.append("requiere revisión")
        return " · ".join(parts)


@dataclass(frozen=True)
class ConstructorSummary:
    has_baseline: bool
    baseline_id: str = ""
    current_id: str = ""
    detected: tuple[DetectedChangeView, ...] = ()
    removed: tuple[str, ...] = ()
    selected: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    baseline_name: str = ""
    baseline_kind: str = ""
    baseline_system: str = ""
    inventory_only: bool = False

    @property
    def exportable_count(self) -> int:
        return sum(1 for item in self.detected if item.exportable)

    @property
    def pending(self) -> tuple[DetectedChangeView, ...]:
        return tuple(item for item in self.detected if item.change_id not in self.selected)


@dataclass(frozen=True)
class GeneratedPlan:
    recipe_yaml: str
    graph: GraphDefinition
    summary: tuple[str, ...]
    details: tuple[str, ...]
    included_ids: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()
    synthesis: SynthesisResult | None = None
    baseline_id: str = ""
    package_id: str = ""


@dataclass(frozen=True)
class PackageBuildResult:
    path: str
    package_id: str
    component_ids: tuple[str, ...]
    baseline_id: str = ""
    baseline_name: str = ""
    skipped: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()


class ChangeConstructorService:
    """Único servicio de producto para construir cambios desde el estado observado."""

    def __init__(self, root: str | Path = ".", home: str | Path | None = None) -> None:
        self.root = Path(root)
        self.home = Path(home or Path.home()).expanduser().resolve()
        self.baselines = BaselineService(root=self.root, home=self.home)
        self._selected: list[str] = []
        self._inventory: Inventory | None = None
        self._inventory_loaded = False
        self._require_scan = False

    def _current_inventory(self) -> Inventory | None:
        """Inventario vigente, leído del disco una sola vez por sesión."""
        if not self._inventory_loaded:
            self._inventory = inventory_mod.latest_inventory(root=self.root)
            self._inventory_loaded = True
        return self._inventory

    def invalidate(self) -> None:
        """Obliga a releer el inventario después de modificar la línea base."""
        self._inventory = None
        self._inventory_loaded = False

    def begin_next_cycle(self) -> ConstructorSummary:
        """Cierra un paquete terminado y prepara una detección nueva.

        La línea base activa se conserva. La selección y el inventario en memoria
        se descartan para que el siguiente ciclo empiece en Detección. Los cambios
        que ya estén representados por un paquete local se filtrarán al escanear de
        nuevo mientras su estado observado siga siendo idéntico.
        """
        self._selected.clear()
        self._inventory = None
        self._inventory_loaded = False
        self._require_scan = True
        definition = self.baselines.active()
        if definition is None:
            return ConstructorSummary(
                has_baseline=False, inventory_only=True,
                warnings=("Listo para iniciar un nuevo ciclo de detección.",),
            )
        return ConstructorSummary(
            has_baseline=True, baseline_id=definition.baseline_id,
            baseline_name=definition.name, baseline_kind=definition.kind.value,
            baseline_system=self.baselines.system_label(definition.system),
            warnings=("Paquete terminado. Escanea para detectar cambios nuevos.",),
        )

    def capture_baseline(self, scope: str = "all", **kwargs) -> ConstructorSummary:
        kind = kwargs.pop("kind", BaselineKind.CUSTOM)
        definition, problems = self.baselines.capture(kind=kind, scope=scope, activate_after=True, **kwargs)
        self.invalidate()
        return ConstructorSummary(
            has_baseline=True, baseline_id=definition.baseline_id, baseline_name=definition.name,
            baseline_kind=definition.kind.value, baseline_system=self.baselines.system_label(definition.system),
            current_id=definition.inventory.inventory_id, problems=self._all_problems(problems),
            warnings=definition.warnings,
        )

    def delete_custom_baseline(self, baseline_id: str) -> None:
        try:
            self.baselines.remove(baseline_id)
        except BaselineError as exc:
            raise ConstructorError(str(exc)) from exc
        self.invalidate()

    def has_baseline(self) -> bool:
        return self.baselines.active() is not None

    def refresh(self, scope: str = "all") -> ConstructorSummary:
        inventory, problems = inventory_mod.scan(scope=scope, home=self.home)
        inventory_mod.save_inventory(inventory, root=self.root)
        self._inventory = inventory
        self._inventory_loaded = True
        self._require_scan = False
        return self._summarize(inventory, problems)

    def summary(self) -> ConstructorSummary:
        if self._require_scan:
            definition = self.baselines.active()
            if definition is None:
                return ConstructorSummary(
                    has_baseline=False, inventory_only=True,
                    warnings=("Escanea para iniciar un nuevo ciclo de detección.",),
                )
            return ConstructorSummary(
                has_baseline=True, baseline_id=definition.baseline_id,
                baseline_name=definition.name, baseline_kind=definition.kind.value,
                baseline_system=self.baselines.system_label(definition.system),
                warnings=("Escanea para detectar cambios nuevos.",),
            )
        inventory = self._current_inventory()
        if inventory is None:
            definition = self.baselines.active()
            if definition is None:
                return ConstructorSummary(has_baseline=False, inventory_only=True)
            return ConstructorSummary(
                has_baseline=True, baseline_id=definition.baseline_id, baseline_name=definition.name,
                baseline_kind=definition.kind.value, baseline_system=self.baselines.system_label(definition.system),
                warnings=("Todavía no hay inventario actual; ejecuta un escaneo.",),
            )
        return self._summarize(inventory, [])

    def _all_problems(self, problems: Sequence[str]) -> tuple[str, ...]:
        return tuple([*problems, *self.baselines.sync_problems])

    @staticmethod
    def _record_fingerprint(record: ApplicationRecord | SystemArtifactRecord) -> str:
        """Firma estable del estado observado que dio origen a un cambio."""
        payload = json.dumps(
            record.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _packaged_fingerprints(self) -> dict[str, set[str]]:
        """Estados ya convertidos en paquetes locales por este Constructor.

        No se ocultan por id a ciegas: si la misma aplicación o archivo cambia de
        versión/contenido, su fingerprint cambia y vuelve a aparecer como pendiente.
        """
        result: dict[str, set[str]] = {}
        for package in PortableLibrary(root=self.root).list_packages():
            manifest = package.manifest
            if manifest.package_type is not PackageType.CHANGE:
                continue
            raw = manifest.metadata.get("source_fingerprints", {})
            if not isinstance(raw, dict):
                continue
            for change_id, fingerprint in raw.items():
                if not isinstance(change_id, str) or not isinstance(fingerprint, str):
                    continue
                result.setdefault(change_id, set()).add(fingerprint)
        return result

    def _already_packaged(
        self, record: ApplicationRecord | SystemArtifactRecord, packaged: dict[str, set[str]]
    ) -> bool:
        change_id = record.app_id if isinstance(record, ApplicationRecord) else record.artifact_id
        return self._record_fingerprint(record) in packaged.get(change_id, set())

    def _summarize(self, inventory: Inventory, problems: Sequence[str]) -> ConstructorSummary:
        definition = self.baselines.active()
        if definition is None:
            detected = tuple(self._app_view(item, "inventario") for item in inventory.applications if is_user_choice(item))
            detected += tuple(self._artifact_view(item, "inventario") for item in inventory.artifacts)
            self._keep_known(detected)
            return ConstructorSummary(
                has_baseline=False, current_id=inventory.inventory_id, detected=detected,
                selected=tuple(self._selected), inventory_only=True,
                problems=self._all_problems(problems),
                warnings=(
                    "Modo inventario: Styler encontró contenido, pero sin línea base no puede afirmar qué es nuevo.",
                ),
            )
        comparison = baseline_mod.compare(definition.inventory, inventory)
        packaged = self._packaged_fingerprints()
        detected = tuple(
            self._app_view(item, "añadido")
            for item in comparison.added
            if is_user_choice(item) and not self._already_packaged(item, packaged)
        )
        detected += tuple(
            self._app_view(after, "cambiado")
            for _before, after in comparison.changed
            if is_user_choice(after) and not self._already_packaged(after, packaged)
        )
        detected += tuple(
            self._artifact_view(item, "añadido")
            for item in comparison.artifacts_added
            if not self._already_packaged(item, packaged)
        )
        detected += tuple(
            self._artifact_view(after, "cambiado")
            for _before, after in comparison.artifacts_changed
            if not self._already_packaged(after, packaged)
        )
        self._keep_known(detected)
        return ConstructorSummary(
            has_baseline=True, baseline_id=definition.baseline_id, baseline_name=definition.name,
            baseline_kind=definition.kind.value, baseline_system=self.baselines.system_label(definition.system),
            current_id=comparison.current_id, detected=detected, selected=tuple(self._selected),
            removed=tuple([x.app_id for x in comparison.removed] + [x.artifact_id for x in comparison.artifacts_removed]),
            warnings=tuple(comparison.warnings), problems=self._all_problems(problems),
        )

    def _keep_known(self, detected: Sequence[DetectedChangeView]) -> None:
        known = {item.change_id for item in detected}
        self._selected = [item for item in self._selected if item in known]

    def _app_view(self, record: ApplicationRecord, role: str) -> DetectedChangeView:
        category = classify(record)
        exportable, reason = can_generate_install(record)
        return DetectedChangeView(
            change_id=record.app_id, name=record.display_name or record.name,
            manager=record.manager, version=record.version, category=category.value,
            category_label=category.human, role=role, confidence=record.origin.confidence.human,
            exportable=exportable, needs_opt_in=False, reason=reason,
            source_kind="application", source_path=record.integrity.artifact_path,
            warnings=tuple(record.warnings),
        )

    def _artifact_view(self, record: SystemArtifactRecord, role: str) -> DetectedChangeView:
        exportable = record.scope == "user"
        return DetectedChangeView(
            change_id=record.artifact_id, name=record.name, manager="archivo",
            version="", category=record.kind.value, category_label=record.kind.human,
            role=role, confidence="Confirmado", exportable=exportable,
            needs_opt_in=not exportable,
            reason=("Se empaquetará con respaldo y verificación." if exportable else
                    "Los recursos del sistema requieren una política privilegiada todavía no definida."),
            source_kind="artifact", source_path=record.path, warnings=tuple(record.warnings),
        )

    def select(
        self, change_ids: Iterable[str], *, allow_review: bool = False
    ) -> ConstructorSummary:
        """Añade cambios y explica inmediatamente por qué alguno no es empaquetable."""
        summary = self.summary()
        known = {item.change_id: item for item in summary.detected}
        for change_id in change_ids:
            item = known.get(change_id)
            if item is None:
                raise ConstructorError(f"No hay un cambio detectado con id {change_id}.")
            if not item.exportable and not allow_review:
                raise ConstructorError(
                    f"«{item.name}» todavía no puede empaquetarse. {item.reason}"
                )
            if change_id not in self._selected:
                self._selected.append(change_id)
        return replace(summary, selected=tuple(self._selected))

    def unselect(self, change_ids: Iterable[str]) -> ConstructorSummary:
        removing = set(change_ids)
        self._selected = [item for item in self._selected if item not in removing]
        return replace(self.summary(), selected=tuple(self._selected))

    def select_all_exportable(self) -> ConstructorSummary:
        return self.select(
            (item.change_id for item in self.summary().detected if item.exportable),
            allow_review=True,
        )

    def clear_selection(self) -> ConstructorSummary:
        self._selected.clear()
        return replace(self.summary(), selected=())

    def _source_records(self) -> tuple[list[ApplicationRecord], list[SystemArtifactRecord], Inventory]:
        inventory = self._current_inventory()
        if inventory is None:
            raise ConstructorError("No hay inventario actual. Ejecuta un escaneo.")
        app_by_id = {item.app_id: item for item in inventory.applications}
        artifact_by_id = {item.artifact_id: item for item in inventory.artifacts}
        apps = [app_by_id[item] for item in self._selected if item in app_by_id]
        artifacts = [artifact_by_id[item] for item in self._selected if item in artifact_by_id]
        return apps, artifacts, inventory

    def _source_fingerprints(self, change_ids: Iterable[str], inventory: Inventory) -> dict[str, str]:
        app_by_id = {item.app_id: item for item in inventory.applications}
        artifact_by_id = {item.artifact_id: item for item in inventory.artifacts}
        fingerprints: dict[str, str] = {}
        for change_id in change_ids:
            record = app_by_id.get(change_id) or artifact_by_id.get(change_id)
            if record is not None:
                fingerprints[change_id] = self._record_fingerprint(record)
        return fingerprints

    def generated_plan(self, package_id: str, name: str, description: str = "") -> GeneratedPlan:
        package_id = normalize_identifier(package_id, fallback=name or "change")
        if not self._selected:
            raise ConstructorError("Selecciona al menos un cambio antes de generar el plan.")
        apps, artifacts, _inventory = self._source_records()
        baseline = self.baselines.active()
        try:
            synthesis = synthesize_recipe(
                package_id, name, apps, artifacts, baseline_id=baseline.baseline_id if baseline else "",
                description=description, home=self.home,
            )
        except ValueError as exc:
            raise ConstructorError(str(exc)) from exc
        workflow = compile_recipe(synthesis.recipe)
        graph = GraphDefinition(
            graph_id=synthesis.recipe.recipe_id, title=synthesis.recipe.name,
            description=synthesis.recipe.description, workflow=workflow,
        )
        details = tuple(
            f"{index}. {step.description or step.id} [{step.step_type}]"
            + (f" depende de {', '.join(step.needs)}" if step.needs else "")
            for index, step in enumerate(workflow.steps, start=1)
        )
        if not synthesis.included_ids:
            raise ConstructorError(
                "Ninguno de los elementos seleccionados pudo convertirse en operaciones. "
                + (synthesis.skipped[0][1] if synthesis.skipped else "")
            )
        summary = (
            f"{len(synthesis.recipe.operations)} operaciones generadas",
            "Checkpoint y recibos reversibles incluidos",
            "Verificación final incluida",
            f"{len(synthesis.assets)} archivos incorporados",
        )
        return GeneratedPlan(
            recipe_yaml=dumps_recipe(synthesis.recipe), graph=graph, summary=summary,
            details=details, included_ids=synthesis.included_ids,
            skipped=synthesis.skipped, warnings=synthesis.warnings,
            synthesis=synthesis,
            baseline_id=baseline.baseline_id if baseline else "",
            package_id=package_id,
        )

    def build_package(
        self, destination: str | Path, package_id: str, name: str,
        version: str = CONSTRUCTOR_PACKAGE_VERSION, description: str = "",
        author: str = "", baseline_id: str = "",
        plan: GeneratedPlan | None = None,
    ) -> PackageBuildResult:
        package_id = normalize_identifier(package_id, fallback=name or "change")
        baseline = self.baselines.get(baseline_id) if baseline_id else self.baselines.active()
        if baseline is None:
            raise ConstructorError("El paquete necesita una línea base objetivo antes de exportarse.")
        inventory = self._current_inventory()
        if inventory is None:
            raise ConstructorError("No hay inventario actual. Ejecuta un escaneo.")
        compatibility = baseline.compatibility_report(inventory.system, scope=CompatibilityScope.GENERAL)
        if compatibility.status is CompatibilityStatus.CONFLICT:
            raise ConstructorError("La línea base objetivo es incompatible: " + compatibility.conflicts[0])
        reusable = (
            plan is not None
            and plan.synthesis is not None
            and plan.package_id == package_id
            and plan.baseline_id == baseline.baseline_id
        )
        if not reusable:
            plan = self.generated_plan(package_id, name, description)
        assert plan is not None and plan.synthesis is not None
        synthesis = plan.synthesis
        recipe_path = f"recipe/{plan.graph.graph_id}.yaml"
        graph_path = f"graph/{plan.graph.graph_id}.json"
        entries: list[ArtifactEntry] = [
            ArtifactEntry("recipe", plan.graph.graph_id, recipe_path, title=name),
            ArtifactEntry("graph", plan.graph.graph_id, graph_path, title=name),
        ]
        contents: dict[str, bytes] = {
            recipe_path: plan.recipe_yaml.encode("utf-8"),
            graph_path: json.dumps(plan.graph.to_dict(), indent=2, ensure_ascii=False).encode("utf-8"),
        }
        for payload in synthesis.assets:
            digest = hashlib.sha256(payload.path.encode("utf-8")).hexdigest()[:20]
            entries.append(ArtifactEntry("asset", f"asset-{digest}", payload.path, title=Path(payload.path).name))
            contents[payload.path] = payload.content
        manifest = PackageManifest(
            package_id=package_id, name=name, version=version, package_type=PackageType.CHANGE,
            description=description or "Cambio construido automáticamente por Styler.", author=author,
            artifacts=tuple(entries),
            metadata={
                "generated_by": "styler.constructor",
                "recipe_id": plan.graph.graph_id,
                "graph_id": plan.graph.graph_id,
                "source_fingerprints": self._source_fingerprints(plan.included_ids, inventory),
                "target_baseline": {
                    "baseline_id": baseline.baseline_id, "name": baseline.name,
                    "kind": baseline.kind.value, "system": baseline.system.to_dict(),
                    "compatibility": compatibility.to_dict(),
                },
            },
        )
        written = build_package(manifest, contents, destination)
        return PackageBuildResult(
            path=str(written), package_id=package_id, component_ids=plan.included_ids,
            baseline_id=baseline.baseline_id, baseline_name=baseline.name,
            skipped=plan.skipped, warnings=plan.warnings,
        )


@dataclass(frozen=True)
class PlanReport:
    """Contenido incluido, omisiones y advertencias del plan, sin ocultarlas."""

    included: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]
    operations: int
    assets: int

    @property
    def is_complete(self) -> bool:
        return not self.skipped

    @property
    def headline(self) -> str:
        if self.is_complete:
            return f"{len(self.included)} elementos incluidos · nada omitido"
        return f"{len(self.included)} incluidos · {len(self.skipped)} OMITIDOS"


def describe_plan(
    plan: GeneratedPlan, names: dict[str, str] | None = None
) -> PlanReport:
    labels = names or {}
    return PlanReport(
        included=tuple(labels.get(item, item) for item in plan.included_ids),
        skipped=tuple((labels.get(item, item), reason) for item, reason in plan.skipped),
        warnings=plan.warnings,
        operations=len(plan.graph.workflow.steps),
        assets=len(plan.synthesis.assets) if plan.synthesis else 0,
    )
