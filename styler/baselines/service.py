"""Caso de uso de líneas base oficiales y personalizadas."""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from typing import Iterable

from styler.provenance import inventory as inventory_mod
from styler.provenance.models import Inventory, SystemIdentity

from . import store
from .models import (
    BaselineDefinition,
    BaselineError,
    BaselineKind,
    CompatibilityScope,
    CompatibilityStatus,
    ImageIdentity,
)
from .runtime import detect_runtime_profile

CATALOG_SENTINEL = "bundled-catalog.json"


def bundled_catalog_entries() -> list:
    """Archivos ``.stylerpkg`` de tipo línea base distribuidos con la aplicación.

    Durante el desarrollo el directorio está vacío o no existe; ninguno de los
    dos casos es un error.
    """
    try:
        catalog = files("styler.baselines").joinpath("catalog")
        return sorted(
            (item for item in catalog.iterdir() if item.name.endswith(".stylerpkg")),
            key=lambda item: item.name,
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError):
        return []


def catalog_signature(candidates: list) -> str:
    """Firma del contenido del catálogo, para no repetir el registro."""
    digest = hashlib.sha256()
    for candidate in candidates:
        digest.update(candidate.name.encode("utf-8"))
        try:
            digest.update(candidate.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def definition_signature(definition: BaselineDefinition) -> str:
    raw = json.dumps(
        definition.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-._")
    return text or "baseline"


def default_baseline_id(system: SystemIdentity, kind: BaselineKind, created_at: float | None = None) -> str:
    date = time.strftime("%Y%m%d", time.localtime(created_at or time.time()))
    pieces = [
        system.distro_id or "linux",
        system.distro_version or "rolling",
        system.distro_variant or system.desktop or "default",
    ]
    # Una baseline oficial identifica una plataforma concreta. Incluir sesión y
    # modelo de release evita colisiones entre, por ejemplo, XFCE/X11 y una
    # futura captura de la misma distro/escritorio bajo otra sesión.
    if kind is BaselineKind.OFFICIAL:
        if system.session_type:
            pieces.append(system.session_type)
        if system.release_model:
            pieces.append(system.release_model)
    pieces.append(system.architecture or "unknown")
    if kind is BaselineKind.CUSTOM:
        pieces.extend(["custom", date])
    elif not system.distro_version:
        pieces.append(date)
    return _slug("-".join(pieces))[:128]


@dataclass(frozen=True)
class BaselineListItem:
    baseline_id: str
    name: str
    kind: str
    kind_label: str
    system_label: str
    active: bool
    recommended: bool
    pending_adoption: bool
    trusted: bool
    created_at: float
    compatibility: str = ""
    compatibility_label: str = ""
    incomplete: bool = False
    loadable: bool = True
    damaged: bool = False
    problem: str = ""

    @property
    def labels(self) -> tuple[str, ...]:
        labels: list[str] = []
        if self.active:
            labels.append("activa")
        elif self.pending_adoption:
            labels.append("pendiente de adopción")
        if self.recommended:
            labels.append("recomendada")
        if self.trusted:
            labels.append("confiable")
        if self.incomplete:
            labels.append("incompleta")
        if self.damaged:
            labels.append("dañada")
        return tuple(labels)


@dataclass(frozen=True)
class BaselineValidationResult:
    baseline_id: str
    valid: bool
    status: str
    issues: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline_id": self.baseline_id,
            "valid": self.valid,
            "status": self.status,
            "issues": list(self.issues),
            "warnings": list(self.warnings),
        }


class BaselineService:
    def __init__(self, root: str | Path = ".", home: str | Path | None = None) -> None:
        self.root = Path(root)
        self.home = Path(home or Path.home()).expanduser().resolve()
        self.sync_problems: list[str] = []
        self.sync_bundled()

    def capture(
        self,
        *,
        kind: BaselineKind = BaselineKind.CUSTOM,
        baseline_id: str = "",
        name: str = "",
        description: str = "",
        author: str = "",
        scope: str = "all",
        installation_profile: str = "default",
        image_name: str = "",
        image_checksum: str = "",
        updates_policy: str = "captured-state",
        clean_install: bool = False,
        captured_after_updates: bool = False,
        activate_after: bool = True,
        trusted: bool = False,
    ) -> tuple[BaselineDefinition, tuple[str, ...]]:
        if kind is BaselineKind.OFFICIAL and not clean_install:
            raise BaselineError(
                "La captura oficial exige confirmar que el sistema proviene de una instalación limpia."
            )
        inventory, problems = inventory_mod.scan(scope=scope, home=self.home)
        inventory_mod.save_inventory(inventory, root=self.root)
        created_at = time.time()
        actual_id = baseline_id or default_baseline_id(inventory.system, kind, created_at)
        if not baseline_id and kind is BaselineKind.CUSTOM:
            actual_id = f"{actual_id}-{inventory.inventory_id}"[:128]
        actual_name = name or self._default_name(inventory.system, kind)
        warnings: list[str] = []
        if kind is BaselineKind.CUSTOM:
            warnings.append(
                "Todo lo presente ahora se considerará preexistente y no aparecerá como cambio respecto a esta línea base."
            )
        if kind is BaselineKind.OFFICIAL and not image_checksum:
            warnings.append(
                "La línea base oficial no declara checksum de la imagen; podrá usarse, pero su origen no es totalmente reproducible."
            )
        definition = BaselineDefinition(
            baseline_id=actual_id,
            name=actual_name,
            kind=kind,
            inventory=inventory,
            description=description,
            author=author,
            created_at=created_at,
            image=ImageIdentity(
                installation_profile=installation_profile,
                image_name=image_name,
                image_checksum=image_checksum,
                updates_policy=updates_policy,
                clean_install=clean_install,
                captured_after_updates=captured_after_updates,
            ),
            runtime=detect_runtime_profile(),
            trusted=trusted,
            source="local-official-authoring" if kind is BaselineKind.OFFICIAL else "local-custom-capture",
            warnings=tuple(warnings),
        )
        store.save(definition, root=self.root)
        if activate_after:
            store.activate(definition.baseline_id, root=self.root)
        return definition, tuple(problems)

    def register_inventory(
        self,
        inventory: Inventory,
        *,
        kind: BaselineKind = BaselineKind.CUSTOM,
        baseline_id: str = "",
        name: str = "",
        clean_install: bool = False,
        activate_after: bool = True,
    ) -> BaselineDefinition:
        """Registra un inventario ya capturado; útil para migración y pruebas."""
        definition = BaselineDefinition(
            baseline_id=baseline_id or (
                f"{default_baseline_id(inventory.system, kind)}-{inventory.inventory_id}"[:128]
                if kind is BaselineKind.CUSTOM
                else default_baseline_id(inventory.system, kind)
            ),
            name=name or self._default_name(inventory.system, kind),
            kind=kind,
            inventory=inventory,
            image=ImageIdentity(clean_install=clean_install),
            runtime=detect_runtime_profile(),
            source="registered-inventory",
        )
        store.save(definition, root=self.root)
        if activate_after:
            store.activate(definition.baseline_id, root=self.root)
        return definition

    def list(self) -> list[BaselineListItem]:
        active = store.active(root=self.root)
        system = inventory_mod.detect_system_identity()
        recommended = store.recommended(system, root=self.root)
        pending_id = ""
        if active is None and recommended is not None:
            pending_id = recommended.baseline_id

        items = [
            BaselineListItem(
                baseline_id=item.baseline_id,
                name=item.name,
                kind=item.kind.value,
                kind_label=item.kind.human,
                system_label=self.system_label(item.system),
                active=bool(active and active.baseline_id == item.baseline_id),
                recommended=bool(recommended and recommended.baseline_id == item.baseline_id),
                pending_adoption=bool(pending_id and pending_id == item.baseline_id),
                trusted=item.trusted,
                created_at=item.created_at,
                compatibility=item.compatibility_report(system).status.value,
                compatibility_label=item.compatibility_report(system).status.human,
                incomplete=item.incomplete_identity(),
            )
            for item in store.list_all(root=self.root)
        ]
        known = {item.baseline_id for item in items}
        for baseline_id, problem in store.broken_entries(root=self.root).items():
            items.append(
                BaselineListItem(
                    baseline_id=baseline_id,
                    name=baseline_id,
                    kind="unknown",
                    kind_label="Desconocida",
                    system_label="No se pudo leer",
                    active=False,
                    recommended=False,
                    pending_adoption=False,
                    trusted=False,
                    created_at=0.0,
                    compatibility=CompatibilityStatus.INCOMPLETE.value,
                    compatibility_label="No evaluable",
                    incomplete=True,
                    loadable=False,
                    damaged=True,
                    problem=problem,
                )
            )
        return items

    def active(self, *, auto_select: bool = True) -> BaselineDefinition | None:
        """Línea base en uso.

        Con ``auto_select`` (por defecto) resuelve el primer arranque: adopta
        únicamente la baseline oficial cuyo sistema declarado coincide con la
        distro, versión y plataforma actuales. Nunca existe un default global.
        pero las rutas de solo lectura deben pasar ``auto_select=False`` para
        no escribir en disco mientras únicamente muestran información.
        """
        selected = store.active(root=self.root)
        if selected is not None:
            return selected
        if not auto_select:
            return None
        default = self.recommended()
        if default is not None:
            return self.activate(default.baseline_id)
        return None

    def sync_bundled(self, *, force: bool = False) -> tuple[str, ...]:
        """Registra las líneas base oficiales incluidas con la aplicación.

        El catálogo puede estar vacío durante el desarrollo. El creador añade
        archivos ``.stylerpkg`` de tipo línea base a ``styler/baselines/catalog`` y el wheel los
        distribuye sin hardcodear distros en Python.

        Se ejecuta una sola vez por contenido del catálogo: un centinela guarda
        la firma de lo que ya se registró, así que construir el servicio en cada
        invocación de la CLI no vuelve a copiar y validar cada archivo.

        Los fallos no se ocultan: quedan en ``self.sync_problems`` para que un
        archivo oficial corrupto sea visible en vez de desaparecer en silencio.
        """
        self.sync_problems = []
        candidates = bundled_catalog_entries()
        signature = catalog_signature(candidates)
        sentinel = self.root / ".styler" / store.BASELINES_DIR / CATALOG_SENTINEL
        previous_bundled_ids = self._sentinel_expected_ids(sentinel)
        if not force and self._sentinel_matches(sentinel, signature):
            expected = self._sentinel_expected_ids(sentinel)
            fingerprints = self._sentinel_fingerprints(sentinel)
            if not candidates or (
                expected
                and fingerprints
                and self._bundled_state_healthy(expected, fingerprints)
            ):
                return ()

        imported: list[str] = []
        bundled_ids: list[str] = []
        bundled_fingerprints: dict[str, str] = {}
        existing_definitions = {
            item.baseline_id: item for item in store.list_all(root=self.root)
        }
        existing = set(existing_definitions)
        previous_signatures = {
            baseline_id: definition_signature(definition)
            for baseline_id, definition in existing_definitions.items()
        }
        for candidate in candidates:
            try:
                raw = candidate.read_bytes()
            except OSError as exc:
                self.sync_problems.append(f"{candidate.name}: no se pudo leer ({exc})")
                continue
            with tempfile.TemporaryDirectory(prefix="styler-catalog-") as staging:
                temporary = Path(staging) / candidate.name
                temporary.write_bytes(raw)
                try:
                    definition = store.import_package(
                        temporary,
                        root=self.root,
                        trust=True,
                        activate_after=False,
                        source_label=f"bundled-catalog:{candidate.name}",
                    )
                except BaselineError as exc:
                    self.sync_problems.append(f"{candidate.name}: {exc}")
                    continue
            current_signature = definition_signature(definition)
            if previous_signatures.get(definition.baseline_id) != current_signature:
                imported.append(definition.baseline_id)
            if definition.baseline_id not in existing:
                existing.add(definition.baseline_id)
            bundled_ids.append(definition.baseline_id)
            bundled_fingerprints[definition.baseline_id] = current_signature

        if not self.sync_problems:
            # El catálogo empacado es autoritativo para las baselines oficiales
            # que suministra Styler. Si una versión retira o reemplaza una, no
            # debe quedar como un supuesto segundo "default" local.
            current_ids = set(bundled_ids)
            for retired_id in previous_bundled_ids:
                if retired_id not in current_ids:
                    store.remove_retired_bundled(retired_id, root=self.root)
            self._write_sentinel(
                sentinel,
                signature,
                bundled_ids,
                bundled_fingerprints,
            )
        return tuple(imported)

    @staticmethod
    def _sentinel_matches(sentinel: Path, signature: str) -> bool:
        try:
            return json.loads(sentinel.read_text(encoding="utf-8")).get("signature") == signature
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    @staticmethod
    def _sentinel_expected_ids(sentinel: Path) -> tuple[str, ...]:
        try:
            data = json.loads(sentinel.read_text(encoding="utf-8"))
            return tuple(str(item) for item in data.get("baseline_ids", []) or [])
        except (OSError, ValueError, TypeError, AttributeError):
            return ()

    @staticmethod
    def _sentinel_fingerprints(sentinel: Path) -> dict[str, str]:
        try:
            data = json.loads(sentinel.read_text(encoding="utf-8"))
            raw = data.get("fingerprints", {})
            if not isinstance(raw, dict):
                return {}
            return {str(key): str(value) for key, value in raw.items()}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _bundled_state_healthy(
        self,
        expected: Iterable[str],
        fingerprints: dict[str, str],
    ) -> bool:
        for baseline_id in expected:
            try:
                definition = store.load(baseline_id, root=self.root)
            except BaselineError:
                return False
            if not definition.is_official or not definition.source.startswith("bundled-catalog:"):
                return False
            if fingerprints.get(baseline_id) != definition_signature(definition):
                return False
        return True

    @staticmethod
    def _write_sentinel(
        sentinel: Path,
        signature: str,
        baseline_ids: Iterable[str],
        fingerprints: dict[str, str],
    ) -> None:
        try:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            temporary = sentinel.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "signature": signature,
                        "baseline_ids": sorted(set(baseline_ids)),
                        "fingerprints": fingerprints,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            temporary.replace(sentinel)
        except OSError:
            # No poder anotar el centinela solo cuesta repetir el registro.
            pass

    def get(self, baseline_id: str) -> BaselineDefinition:
        return store.load(baseline_id, root=self.root)

    def validate(self, baseline_id: str) -> BaselineValidationResult:
        """Diagnóstico legible de una línea base ya registrada."""
        try:
            definition = store.load(baseline_id, root=self.root)
        except BaselineError as exc:
            return BaselineValidationResult(
                baseline_id=baseline_id,
                valid=False,
                status="damaged",
                issues=(str(exc),),
            )

        issues: list[str] = []
        warnings: list[str] = list(definition.warnings)
        if not definition.inventory.inventory_id:
            issues.append("El inventario no tiene identificador.")
        if definition.incomplete_identity():
            warnings.append(
                "La identidad no declara distribución, versión y arquitectura completas."
            )
        if definition.is_official:
            if not definition.image.image_checksum:
                warnings.append("La imagen original no declara checksum.")
            if not definition.trusted:
                warnings.append("La línea base oficial no está marcada como confiable.")
        if not definition.runtime.python.provider:
            warnings.append("No se registró el proveedor de Python usado por Styler.")
        return BaselineValidationResult(
            baseline_id=baseline_id,
            valid=not issues,
            status="healthy" if not issues and not warnings else ("warning" if not issues else "damaged"),
            issues=tuple(dict.fromkeys(issues)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def validate_all(self) -> tuple[BaselineValidationResult, ...]:
        ids = [item.baseline_id for item in store.list_all(root=self.root)]
        ids.extend(store.broken_entries(root=self.root))
        return tuple(self.validate(baseline_id) for baseline_id in dict.fromkeys(ids))

    def repair_catalog(self) -> tuple[str, ...]:
        """Restaura las líneas base oficiales incluidas y vuelve a validarlas."""
        repaired = self.sync_bundled(force=True)
        if self.sync_problems:
            raise BaselineError(
                "No se pudo reparar todo el catálogo oficial: " + "; ".join(self.sync_problems)
            )
        return repaired

    def activate(self, baseline_id: str) -> BaselineDefinition:
        definition = store.activate(baseline_id, root=self.root)
        return definition

    def recommended(self, system: SystemIdentity | None = None) -> BaselineDefinition | None:
        return store.recommended(system or inventory_mod.detect_system_identity(), root=self.root)

    def activate_recommended(self) -> BaselineDefinition:
        definition = self.recommended()
        if definition is None:
            raise BaselineError(
                "No hay una línea base oficial compatible con esta distribución, versión, edición y arquitectura."
            )
        return self.activate(definition.baseline_id)

    def import_package(self, source: str | Path, *, activate_after: bool = False, trust: bool = False) -> BaselineDefinition:
        return store.import_package(
            source,
            root=self.root,
            activate_after=activate_after,
            trust=trust,
        )

    def export_package(self, baseline_id: str, destination: str | Path) -> Path:
        return store.export_package(baseline_id, destination, root=self.root)

    def export_catalog_candidate(
        self,
        baseline_id: str,
        destination: str | Path,
        *,
        clean_install_confirmed: bool = False,
    ) -> Path:
        """Crea un paquete oficial candidato sin mutar la línea base local.

        El catálogo integrado solo recomienda definiciones ``official``. La
        confirmación es obligatoria porque marcar una captura cotidiana como
        instalación limpia convertiría aplicaciones personales en parte del
        supuesto estado original de la distribución.
        """
        if not clean_install_confirmed:
            raise BaselineError(
                "Confirma explícitamente que la captura procede de una instalación limpia."
            )
        source = self.get(baseline_id)
        official_id = default_baseline_id(source.system, BaselineKind.OFFICIAL, source.created_at)
        clean_image = replace(source.image, clean_install=True)
        warnings = tuple(
            warning for warning in source.warnings
            if "Todo lo presente ahora se considerará preexistente" not in warning
        )
        candidate = replace(
            source,
            baseline_id=official_id,
            name=self._default_name(source.system, BaselineKind.OFFICIAL),
            kind=BaselineKind.OFFICIAL,
            image=clean_image,
            trusted=False,
            source="local-official-authoring",
            warnings=warnings,
        )
        target = Path(destination).expanduser()
        if target.suffix.lower() != ".stylerpkg":
            target = target / f"{candidate.baseline_id}.stylerpkg"
        return store.export_definition_package(candidate, target)

    def remove(self, baseline_id: str) -> None:
        store.remove(baseline_id, root=self.root)

    def compare(self, current: Inventory, baseline_id: str = ""):
        definition = self.get(baseline_id) if baseline_id else self.active()
        if definition is None:
            raise BaselineError("No hay una línea base activa.")
        from styler.provenance.baseline import compare
        return compare(definition.inventory, current)

    @staticmethod
    def system_label(system: SystemIdentity) -> str:
        pieces = [system.distro_id or "Linux"]
        if system.distro_version:
            pieces.append(system.distro_version)
        if system.distro_variant:
            pieces.append(system.distro_variant)
        elif system.desktop:
            pieces.append(system.desktop)
        if system.desktop and system.distro_variant and system.desktop.lower() not in system.distro_variant.lower():
            pieces.append(system.desktop)
        if system.desktop_version:
            pieces.append(f"desktop {system.desktop_version}")
        if system.session_type:
            pieces.append(system.session_type)
        if system.release_model:
            pieces.append(system.release_model)
        if system.architecture:
            pieces.append(system.architecture)
        return " · ".join(pieces)

    @staticmethod
    def _default_name(system: SystemIdentity, kind: BaselineKind) -> str:
        label = BaselineService.system_label(system)
        return f"{label} — {'oficial' if kind is BaselineKind.OFFICIAL else 'personalizada'}"
