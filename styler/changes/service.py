"""Servicio de cambios semánticos de Styler.

PhotoGIMP es el primer cambio completamente descrito. La implementación
reutiliza el catálogo, el resolver, el compilador y el motor DAG existentes,
pero presenta una sola intención al usuario y elige una estrategia automática
o asistida según el proveedor de GIMP.
"""
from __future__ import annotations

import json
import hashlib
import os
import errno
import tempfile
import platform
import re
import shutil
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from styler.component_catalog.compiler import compile_workflow
from styler.change_recipe import compile_recipe
from styler.declarative_changes import dependency_order, load_declarative_changes
from styler.component_catalog.executors import PHOTOGIMP_RELEASE_PREFIX, extended_registry
from styler.component_catalog.loader import load
from styler.component_catalog.registry import ComponentRegistry
from styler.component_catalog.resolver import resolve
from styler.receipts import (
    ReceiptJournal,
    ReceiptKind,
    StepReceipt,
    all_checkpoint_receipts,
    compile_rollback_workflow,
    prune_system_checkpoints,
)
from styler.methods import (
    MethodContext,
    MethodPolicy,
    annotate_workflow_methods,
    default_method_registry,
)
from styler.portable import GraphDefinition, InstalledPackage, PackageType, PortableLibrary
from styler.privileges import keepalive_for
from styler.runtime.engine import WorkflowEngine
from styler.runtime.graph import drop_step, topological_order
from styler.runtime.models import ExecutionContext, PhaseDefinition, StepDefinition, WorkflowDefinition
from styler.target import detect_target

from .models import (
    AutomationLevel,
    BatchProgressCallback,
    ChangeBatchExecutionResult,
    ChangeBatchPlan,
    ChangeBatchProgressEvent,
    ChangeCard,
    ChangeExecutionResult,
    ChangeOption,
    ChangePhase,
    ChangePlan,
    ChangeProgressEvent,
    ChangeWorkflowPair,
    ChangeStatus,
    ProgressCallback,
    ProviderOption,
)


PROVIDER_LABELS = {
    "flatpak": "Flathub (Flatpak)",
    "apt": "APT",
    "pacman": "Pacman",
    "aur": "AUR",
    "rpm": "DNF/RPM",
    "zypper": "Zypper",
    "snap": "Snap",
    "appimage": "AppImage",
}

PROVIDER_COMMANDS = {
    "flatpak": ("flatpak",),
    "apt": ("apt-get", "dpkg-query"),
    "pacman": ("pacman",),
    "aur": ("yay", "paru"),
    "rpm": ("dnf", "rpm"),
    "zypper": ("zypper",),
    "snap": ("snap",),
    "appimage": (),
}

# Carpetas de configuración de GIMP: 3.0, 3.2, 4.0, 10.4… No se codifica la
# familia 3.x, para que una versión mayor futura se detecte igual.
_CONFIG_VERSION_DIR = re.compile(r"\d+\.\d+")



class ChangeStateWriteError(OSError):
    """Styler no pudo persistir el estado del cambio con seguridad."""

    def __init__(self, path: Path, original: OSError) -> None:
        self.path = Path(path)
        self.original = original
        super().__init__(getattr(original, "errno", None), str(original), str(path))


def _mount_status(path: Path) -> str:
    """Describe el montaje que contiene *path* sin depender de comandos externos."""
    try:
        candidate = path.resolve(strict=False)
        best: tuple[int, str, str] | None = None
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            left, _sep, right = line.partition(" - ")
            fields = left.split()
            if len(fields) < 6:
                continue
            mountpoint = Path(fields[4].replace("\\040", " "))
            options = fields[5]
            try:
                candidate.relative_to(mountpoint)
            except ValueError:
                continue
            score = len(str(mountpoint))
            fs_type = right.split()[0] if right else "?"
            if best is None or score > best[0]:
                best = (score, str(mountpoint), f"{options}; fs={fs_type}")
        if best is not None:
            return f"montaje={best[1]} · {best[2]}"
    except OSError:
        pass
    return "montaje no disponible"


CONTINUATION_STATUSES = {
    ChangeStatus.FAILED,
    ChangeStatus.INTEGRATING,
    ChangeStatus.NEEDS_ATTENTION,
}


CHANGE_NAMES = {
    "photogimp": "PhotoGIMP",
}


def _change_name(change_id: str) -> str:
    """Nombre legible sin acoplar la reversión a un pipeline concreto."""
    return CHANGE_NAMES.get(change_id, change_id.replace("-", " ").title())


class ChangeService:
    """Construye, ejecuta y registra cambios sin exponer componentes internos."""

    def __init__(self, root: str | Path = ".", home: str | Path | None = None) -> None:
        self.root = Path(root)
        self.home = Path(home).expanduser() if home else Path.home()
        self.root.mkdir(parents=True, exist_ok=True)
        self._state_dir = self.root / "styler"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._preferences_path = self._state_dir / "preferences.json"
        self._records_path = self._state_dir / "change-records.json"
        self._portable_library = PortableLibrary(root=self.root)
        self._declarative_changes = load_declarative_changes()
        self._registry = ComponentRegistry.from_report(load(root=self.root))
        self._target = detect_target(root=str(self.root))
        self._method_registry = default_method_registry()
        self._method_policy = MethodPolicy(
            prefer_terminal=True,
            allow_gui_input=False,
            require_reversible=False,
            allow_privileged=True,
        )

    @staticmethod
    def _storage_error(exc: OSError, fallback: Path) -> ChangeStateWriteError:
        """Conserva errno/path del error real aunque venga envuelto por recibos."""
        current: BaseException | None = exc
        chosen: OSError = exc
        seen: set[int] = set()
        while isinstance(current, BaseException) and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, OSError):
                chosen = current
                if getattr(current, "errno", None) in {errno.EROFS, errno.EACCES, errno.EPERM}:
                    break
            current = current.__cause__ or current.__context__
        filename = getattr(chosen, "filename", None)
        return ChangeStateWriteError(Path(filename) if filename else fallback, chosen)

    @staticmethod
    def _probe_directory_writable(path: Path) -> None:
        """Prueba una escritura real y atómica, no solo os.access()."""
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / f".styler-write-probe-{os.getpid()}-{time.time_ns()}"
            with probe.open("x", encoding="utf-8") as handle:
                handle.write("styler write probe\n")
                handle.flush()
                os.fsync(handle.fileno())
            probe.unlink()
        except OSError as exc:
            try:
                if 'probe' in locals():
                    probe.unlink(missing_ok=True)
            except OSError:
                pass
            raise ChangeStateWriteError(path, exc) from exc

    def _assert_execution_storage_writable(self, change_id: str) -> None:
        """Comprueba todos los almacenes que PipeCraft/rollback necesitan escribir.

        El lote no tiene almacenamiento propio: cada DAG usa exactamente estos
        mismos directorios. Probarlos antes evita iniciar efectos si HOME/root
        ya fue remontado en solo lectura.
        """
        for directory in (
            self._state_dir,
            self.root / ".styler" / "runs",
            self.root / ".styler" / "receipts",
            # PipeCraft 1.5 mantiene su journal/job store en un workspace
            # privado. Se prueba antes de cualquier efecto igual que receipts.
            self.root / ".styler" / "pipecraft" / ".pipelines",
        ):
            self._probe_directory_writable(directory)
        try:
            self.journal_for_change(change_id).ensure_writable()
        except OSError as exc:
            raise self._storage_error(
                exc, self.root / ".styler" / "receipts" / f"{change_id}.jsonl"
            ) from exc

    @staticmethod
    def _is_storage_failure(exc: OSError) -> bool:
        current: BaseException | None = exc
        seen: set[int] = set()
        while isinstance(current, BaseException) and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, OSError) and getattr(current, "errno", None) in {
                errno.EROFS, errno.EACCES, errno.EPERM
            }:
                return True
            current = current.__cause__ or current.__context__
        return False

    # ------------------------------------------------------------------ public
    def available_changes(self) -> tuple[ChangeCard, ...]:
        """Todos los cambios aplicables, sin exponer cómo fueron definidos.

        PhotoGIMP sigue usando su pipeline YAML/catálogo de componentes. Los
        ``.stylerpkg`` aportan DAG portables. Ambos convergen aquí como
        ``ChangeCard`` y, desde este punto, usan el mismo flujo de Cambios.
        """
        cards: list[ChangeCard] = [self._photogimp_available_card()]
        records = self._load_records()
        for change_id, change in self._declarative_changes.items():
            compatibility_error = change.compatibility_error(
                family=self._target.family, architecture=platform.machine(),
            )
            if compatibility_error:
                continue
            record = records.get(change_id, {})
            status = str(record.get("status") or "") if isinstance(record, dict) else ""
            retry = status in CONTINUATION_STATUSES
            requirement_note = (
                " Requiere: " + ", ".join(change.requires_changes) + "."
                if change.requires_changes else ""
            )
            cards.append(
                ChangeCard(
                    change_id=change_id,
                    name=change.recipe.name,
                    description=change.description,
                    category=change.category,
                    status=ChangeStatus.AVAILABLE,
                    status_label="Reintento disponible" if retry else "Disponible",
                    provider_id="yaml",
                    provider_label=change.provider_label,
                    automation_level=AutomationLevel.AUTOMATIC,
                    detail=f"DAG declarado en {change.source.name}.{requirement_note}",
                    warning="",
                    reversible=self.can_rollback(change_id),
                    continuation_available=False,
                )
            )
        for change_id, package, graph in self._portable_change_sources():
            record = records.get(change_id, {})
            status = str(record.get("status") or "") if isinstance(record, dict) else ""
            retry = status in CONTINUATION_STATUSES
            detail = (
                f"DAG «{graph.title}» · {len(graph.workflow.steps)} paso(s) · "
                f"{package.identity}."
            )
            if status == ChangeStatus.INTEGRATED:
                detail += " Ya fue integrado; puedes volver a aplicarlo como reparación."
            elif retry:
                detail += " Existe una ejecución incompleta; puedes revisar el DAG y volver a intentarlo."
            cards.append(
                ChangeCard(
                    change_id=change_id,
                    name=(package.manifest.name if self._package_graph_count(package) == 1 else graph.title),
                    description=graph.description or package.manifest.description or "Cambio importado en formato .stylerpkg.",
                    category="Paquete .stylerpkg · DAG",
                    status=ChangeStatus.AVAILABLE,
                    status_label="Reintento disponible" if retry else "Disponible",
                    provider_id="stylerpkg",
                    provider_label="DAG de paquete .stylerpkg",
                    automation_level=AutomationLevel.AUTOMATIC,
                    detail=detail,
                    warning="",
                    reversible=self.can_rollback(change_id),
                    continuation_available=False,
                )
            )
        return tuple(cards)

    def integrated_changes(self) -> tuple[ChangeCard, ...]:
        cards = list(self._photogimp_integrated_cards())
        records = self._load_records()
        live_sources = {change_id: (package, graph) for change_id, package, graph in self._portable_change_sources()}
        for change_id, record in records.items():
            if not change_id.startswith("pkg.") or not isinstance(record, dict):
                continue
            status = str(record.get("status", ChangeStatus.UNKNOWN))
            if status not in {
                ChangeStatus.INTEGRATED,
                ChangeStatus.PREPARED,
                ChangeStatus.FAILED,
                ChangeStatus.NEEDS_ATTENTION,
                ChangeStatus.PARTIALLY_REVERTED,
                ChangeStatus.REVERTING,
                ChangeStatus.INTEGRATING,
            }:
                continue
            package_graph = live_sources.get(change_id)
            if package_graph is not None:
                package, graph = package_graph
                name = str(record.get("name") or package.manifest.name or graph.title)
                description = graph.description or package.manifest.description or "Cambio portable registrado por Styler."
                detail = str(record.get("message") or f"DAG «{graph.title}» desde {package.identity}.")
            else:
                name = str(record.get("name") or _change_name(change_id))
                description = "Cambio portable registrado por Styler."
                detail = str(record.get("message") or "El paquete ya no está en la biblioteca local.")
            cards.append(
                ChangeCard(
                    change_id=change_id,
                    name=name,
                    description=description,
                    category="Paquete .stylerpkg · DAG",
                    status=status,
                    status_label=self._status_label(status),
                    provider_id="stylerpkg",
                    provider_label="DAG de paquete .stylerpkg",
                    automation_level=str(record.get("automation_level", AutomationLevel.AUTOMATIC)),
                    detail=detail,
                    reversible=self.can_rollback(change_id),
                    detected_at=float(record.get("updated_at", 0.0)),
                )
            )
        for change_id, change in self._declarative_changes.items():
            record = records.get(change_id, {})
            if not isinstance(record, dict):
                continue
            status = str(record.get("status") or ChangeStatus.UNKNOWN)
            if status not in {
                ChangeStatus.INTEGRATED, ChangeStatus.PREPARED, ChangeStatus.FAILED,
                ChangeStatus.NEEDS_ATTENTION, ChangeStatus.PARTIALLY_REVERTED,
                ChangeStatus.REVERTING, ChangeStatus.INTEGRATING,
            }:
                continue
            cards.append(
                ChangeCard(
                    change_id=change_id,
                    name=str(record.get("name") or change.recipe.name),
                    description=change.description,
                    category=change.category,
                    status=status,
                    status_label=self._status_label(status),
                    provider_id="yaml",
                    provider_label=change.provider_label,
                    automation_level=str(record.get("automation_level") or AutomationLevel.AUTOMATIC),
                    detail=str(record.get("message") or f"DAG YAML incorporado: {change.source.name}."),
                    reversible=self.can_rollback(change_id),
                    detected_at=float(record.get("updated_at", 0.0)),
                )
            )
        return tuple(cards)

    def _photogimp_available_card(self) -> ChangeCard:
        provider = self.provider_for("photogimp")
        option = self._provider_option(provider)
        integrated = {item.change_id: item for item in self._photogimp_integrated_cards()}
        current = integrated.get("photogimp")
        record = self._load_records().get("photogimp", {})
        previous_status = str(record.get("status") or "") if isinstance(record, dict) else ""
        continuation = previous_status in CONTINUATION_STATUSES
        if current and current.status == ChangeStatus.INTEGRATED:
            detail = (
                "PhotoGIMP ya está integrado; puedes revisar su estrategia o volver a "
                "integrarlo si necesitas reparar la configuración."
            )
        elif continuation:
            detail = (
                "Hay una integración incompleta. Styler comprobará el estado real, "
                "reutilizará lo ya terminado y continuará desde el primer paso pendiente."
            )
        else:
            detail = "Instala GIMP y adapta su interfaz mediante un pipeline verificable y reversible."
        return ChangeCard(
            change_id="photogimp",
            name="PhotoGIMP",
            description="Convierte GIMP en una experiencia de trabajo similar a Photoshop.",
            category="Creatividad · GIMP · Interfaz",
            status=ChangeStatus.AVAILABLE,
            status_label="Continuación disponible" if continuation else "Disponible",
            provider_id=provider,
            provider_label=option.label,
            automation_level=option.automation_level,
            detail=detail,
            warning=option.warning,
            reversible=provider == "flatpak",
            continuation_available=continuation,
        )

    def _photogimp_integrated_cards(self) -> tuple[ChangeCard, ...]:
        detected = self._detect_photogimp()
        records = self._load_records()
        record = records.get("photogimp", {})
        if detected is not None:
            provider_id, marker = detected
            return (
                ChangeCard(
                    change_id="photogimp",
                    name="PhotoGIMP",
                    description="Adaptación de GIMP detectada en este equipo.",
                    category="Creatividad · GIMP · Interfaz",
                    status=ChangeStatus.INTEGRATED,
                    status_label="Integrado",
                    provider_id=provider_id,
                    provider_label=PROVIDER_LABELS.get(provider_id, provider_id),
                    automation_level=(
                        AutomationLevel.AUTOMATIC
                        if provider_id == "flatpak"
                        else AutomationLevel.ASSISTED
                    ),
                    detail=f"Marcador verificado en {marker}",
                    reversible=bool(record.get("reversible", provider_id == "flatpak")),
                    detected_at=float(record.get("updated_at", marker.stat().st_mtime)),
                ),
            )
        if record:
            status = str(record.get("status", ChangeStatus.UNKNOWN))
            if status in {
                ChangeStatus.PREPARED,
                ChangeStatus.FAILED,
                ChangeStatus.NEEDS_ATTENTION,
                ChangeStatus.PARTIALLY_REVERTED,
                ChangeStatus.REVERTING,
                ChangeStatus.INTEGRATING,
            }:
                provider = str(record.get("provider_id", ""))
                handoff = str(record.get("handoff_path", ""))
                return (
                    ChangeCard(
                        change_id="photogimp",
                        name="PhotoGIMP",
                        description="Estado registrado por Styler.",
                        category="Creatividad · GIMP · Interfaz",
                        status=status,
                        status_label=self._status_label(status),
                        provider_id=provider,
                        provider_label=PROVIDER_LABELS.get(provider, provider),
                        automation_level=str(record.get("automation_level", AutomationLevel.ASSISTED)),
                        detail=(
                            f"Archivo preparado en {handoff}"
                            if handoff
                            else str(record.get("message", ""))
                        ),
                        reversible=bool(record.get("reversible", False)),
                        detected_at=float(record.get("updated_at", 0.0)),
                    ),
                )
        return ()

    def provider_options(self, change_id: str) -> tuple[ProviderOption, ...]:
        self._require_change(change_id)
        if self._is_portable_change(change_id):
            return (
                ProviderOption(
                    provider_id="stylerpkg",
                    label="DAG de paquete .stylerpkg",
                    description="El cambio ejecuta el DAG declarativo contenido en el paquete importado.",
                    automation_level=AutomationLevel.AUTOMATIC,
                    recommended=True,
                    available=True,
                ),
            )
        component = self._registry.get("app.gimp")
        if component is None:
            return ()
        options: list[ProviderOption] = []
        for provider in component.providers:
            if provider.id == "appimage":
                # No existe todavía una fuente AppImage oficial declarada que
                # Styler pueda descargar de manera reproducible.
                continue
            if provider.families and self._target.family not in provider.families and "*" not in provider.families:
                continue
            automatic = provider.id == "flatpak"
            available = self._provider_command_available(provider.id)
            warning = ""
            if not automatic:
                warning = (
                    "PhotoGIMP recomienda GIMP desde Flathub. Con esta fuente Styler "
                    "instalará GIMP y descargará PhotoGIMP, pero la integración quedará manual."
                )
            if not available:
                warning = (warning + " " if warning else "") + (
                    "El gestor correspondiente no fue detectado; Styler mostrará el fallo de preparación si sigue ausente."
                )
            options.append(
                ProviderOption(
                    provider_id=provider.id,
                    label=PROVIDER_LABELS.get(provider.id, provider.id),
                    description=(
                        "Integración automática, respaldo y verificación completos."
                        if automatic
                        else "Instalación asistida: GIMP automático y PhotoGIMP preparado en Descargas."
                    ),
                    automation_level=(
                        AutomationLevel.AUTOMATIC if automatic else AutomationLevel.ASSISTED
                    ),
                    recommended=automatic,
                    available=available,
                    warning=warning,
                )
            )
        options.sort(key=lambda item: (not item.recommended, item.label.lower()))
        return tuple(options)

    def provider_for(self, change_id: str) -> str:
        self._require_change(change_id)
        if self._is_portable_change(change_id):
            return "stylerpkg"
        preferences = self._load_preferences()
        selected = str(preferences.get("providers", {}).get(change_id, ""))
        valid = {option.provider_id for option in self.provider_options(change_id)}
        return selected if selected in valid else "flatpak"

    def set_provider(self, change_id: str, provider_id: str) -> None:
        if self._is_portable_change(change_id):
            if provider_id != "stylerpkg":
                raise ValueError("Un cambio portable usa el DAG contenido en su .stylerpkg.")
            return
        valid = {option.provider_id for option in self.provider_options(change_id)}
        if provider_id not in valid:
            raise ValueError(f"El proveedor '{provider_id}' no está disponible para este equipo.")
        preferences = self._load_preferences()
        providers = dict(preferences.get("providers", {}))
        providers[change_id] = provider_id
        preferences["providers"] = providers
        self._write_json(self._preferences_path, preferences)

    PHOTOGIMP_OPTIONS: tuple[ChangeOption, ...] = (
        ChangeOption(
            "backup",
            "Respaldar la configuración actual de GIMP",
            "Copia tu configuración antes de tocarla. Sin respaldo, deshacer solo "
            "puede quitar lo que Styler escribió, no devolver lo que había.",
            default=True,
        ),
        ChangeOption(
            "rewrite_launchers",
            "Adaptar el acceso del menú",
            "Reescribe el lanzador de PhotoGIMP para que abra GIMP Flatpak.",
            default=True,
            advanced=True,
        ),
        ChangeOption(
            "startup_timeout_seconds",
            "Tiempo máximo de arranque de GIMP",
            "Cuánto esperar a que GIMP complete su primer arranque. Styler conserva "
            "cada hito observado; este límite solo se agota si falta uno de ellos.",
            kind="number",
            default=90.0,
            minimum=15.0,
            maximum=300.0,
            advanced=True,
        ),
    )

    def options_for(self, change_id: str) -> tuple[ChangeOption, ...]:
        self._require_change(change_id)
        if self._is_portable_change(change_id):
            return ()
        return self.PHOTOGIMP_OPTIONS

    def default_options(self, change_id: str) -> dict[str, Any]:
        return {option.option_id: option.default for option in self.options_for(change_id)}

    def normalize_options(self, change_id: str, values: dict[str, Any] | None) -> dict[str, Any]:
        """Toda opción desconocida se descarta y toda opción fuera de rango se
        recorta: un paquete importado no puede introducir ajustes nuevos."""
        resolved = self.default_options(change_id)
        for option in self.options_for(change_id):
            if values and option.option_id in values:
                resolved[option.option_id] = option.coerce(values[option.option_id])
        return resolved

    def build_plan(
        self,
        change_id: str,
        provider_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ChangePlan:
        self._require_change(change_id)
        if self._is_portable_change(change_id):
            return self._build_portable_plan(change_id)
        if change_id in self._declarative_changes:
            return self._build_declarative_plan(change_id)
        provider_id = provider_id or self.provider_for(change_id)
        resolved_options = self.normalize_options(change_id, options)
        option = self._provider_option(provider_id)
        continuation_mode = self._continuation_mode(change_id)
        if provider_id == "flatpak":
            workflow = self._build_automatic_photogimp(provider_id)
            workflow = self._with_initial_checkpoint(workflow, change_id)
            workflow = self._apply_options(workflow, resolved_options)
            automatic = True
            summary = (
                "Styler instalará GIMP desde Flathub, lo abrirá una vez, respaldará su "
                "configuración, integrará la última publicación de PhotoGIMP y volverá "
                "a abrir GIMP para confirmar que arranca adaptado."
            )
            notice = "Flathub es la estrategia recomendada y comprobada para PhotoGIMP."
            level = AutomationLevel.AUTOMATIC
        else:
            workflow = self._build_assisted_photogimp(provider_id)
            workflow = self._with_initial_checkpoint(workflow, change_id)
            automatic = False
            summary = (
                f"Styler instalará GIMP mediante {option.label} y descargará PhotoGIMP en "
                "la carpeta de Descargas. La copia final deberá hacerse manualmente."
            )
            notice = option.warning
            level = AutomationLevel.ASSISTED

        reconciliation_context = ExecutionContext(
            root=self.root,
            dry_run=True,
            approve=False,
            values={
                "home": str(self.home),
                "change_id": change_id,
                "continuation_mode": continuation_mode,
            },
        )
        reconciled_results = WorkflowEngine(extended_registry()).reconciliation(
            workflow, reconciliation_context
        )
        reconciled_steps = {
            step_id: {
                "status": result.status,
                "message": result.message,
                "data": dict(result.data),
            }
            for step_id, result in reconciled_results.items()
        }
        phases = self._phases_for_workflow(workflow, automatic=automatic)
        phases = self._decorate_reconciled_phases(phases, reconciled_results)

        if "app.gimp.install" in reconciled_results:
            if automatic:
                summary = (
                    "GIMP ya está instalado y no se volverá a instalar. Styler continuará "
                    "desde la inicialización o desde el primer paso que siga pendiente, "
                    "respaldará la configuración e integrará PhotoGIMP."
                )
            else:
                summary = (
                    f"GIMP ya está instalado y se reutilizará. Styler continuará con la "
                    f"preparación de PhotoGIMP para {option.label}."
                )
        if continuation_mode and reconciled_results:
            notice = self._join_notice(
                notice,
                f"Continuación detectada: {len(reconciled_results)} paso(s) ya completado(s) "
                "se reutilizarán sin repetir sus efectos.",
            )

        weights = {phase.step_id: phase.weight for phase in phases}
        labels = {phase.step_id: phase.label for phase in phases}
        workflow.operation = "apply"
        workflow.metadata.update(
            {
                "max_workers": 1,
                "change_id": change_id,
                "change_name": "PhotoGIMP",
                "progress_weights": weights,
                "progress_labels": labels,
                "options": dict(resolved_options),
                "continuation_mode": continuation_mode,
                "reconciled_steps": reconciled_steps,
                "dag_role": "apply",
            }
        )
        workflow = annotate_workflow_methods(
            workflow,
            registry=self._method_registry,
            context=MethodContext.detect(),
            policy=self._method_policy,
        )
        undo_workflow = self.rollback_plan(change_id) if self.can_rollback(change_id) else None
        return ChangePlan(
            change_id=change_id,
            name="PhotoGIMP",
            provider_id=provider_id,
            provider_label=option.label,
            automation_level=level,
            summary=summary,
            notice=notice,
            phases=phases,
            workflow=workflow,
            undo_workflow=undo_workflow,
            options=resolved_options,
            reconciled_steps=reconciled_steps,
            continuation_mode=continuation_mode,
            operation="apply",
        )

    def build_removal_plan(self, change_id: str) -> ChangePlan:
        """Construye el DAG exacto para retirar un cambio instalado.

        El plan no invierte el Apply DAG. Se compila desde recibos de efectos
        reales: archivos creados, archivos sustituidos, respaldos y paquetes
        que Styler demostró haber instalado.
        """
        self._validate_change_id(change_id)
        if not self.can_rollback(change_id):
            raise ValueError(
                "Styler no tiene efectos vivos registrados para quitar este cambio. "
                "No se ejecutará una desinstalación por conjetura."
            )

        workflow = self.rollback_plan(change_id)
        record = self._load_records().get(change_id, {})
        name = str(record.get("name") or _change_name(change_id))
        provider_id = str(record.get("provider_id") or "recorded")
        provider_label = str(
            record.get("provider_label") or PROVIDER_LABELS.get(provider_id, "Registrado por Styler")
        )
        phases = self._phases_for_removal(workflow)
        weights = {phase.step_id: phase.weight for phase in phases}
        labels = {phase.step_id: phase.label for phase in phases}
        workflow.operation = "undo"
        workflow.metadata.update(
            {
                "max_workers": 4,
                "change_id": change_id,
                "change_name": name,
                "progress_weights": weights,
                "progress_labels": labels,
                "dag_role": "undo",
                "user_intent": "remove-change",
            }
        )

        summary, notice = self._removal_summary(workflow)
        return ChangePlan(
            change_id=change_id,
            name=name,
            provider_id=provider_id,
            provider_label=provider_label,
            automation_level=str(
                record.get("automation_level") or AutomationLevel.AUTOMATIC
            ),
            summary=summary,
            notice=notice,
            phases=phases,
            workflow=workflow,
            undo_workflow=workflow,
            options={},
            reconciled_steps={},
            continuation_mode=False,
            operation="remove",
        )

    @staticmethod
    def _workflow_requires_admin(workflow: WorkflowDefinition) -> bool:
        """True cuando el DAG contiene una operación de sistema que necesita root.

        No cambia el DAG ni elige otro proveedor; solo permite que la interfaz
        solicite autorización antes de que ``sudo -n`` tenga que fallar.
        """
        privileged_managers = {"apt", "pacman", "aur", "rpm", "zypper", "snap"}
        for step in workflow.steps:
            satisfied_by = step.config.get("satisfied_by")
            if isinstance(satisfied_by, dict):
                executable = str(satisfied_by.get("executable") or "").strip()
                if executable and shutil.which(executable):
                    # El YAML declaró explícitamente que esta capacidad existente
                    # satisface el paso. No se pedirá sudo para una instalación
                    # que el ejecutor reconciliará sin tocar el sistema.
                    continue
            if step.step_type == "install_package":
                package = dict(step.config.get("package") or {})
                if str(package.get("manager") or "") in privileged_managers:
                    return True
            elif step.step_type == "install_package_artifact":
                if str(step.config.get("manager") or "") in privileged_managers:
                    return True
            elif step.step_type == "uninstall_package":
                if str(step.config.get("manager") or "") in privileged_managers:
                    return True
            elif step.step_type == "enable_service":
                service = dict(step.config.get("service") or {})
                if str(service.get("scope") or "user") != "user":
                    return True
        return False

    def plan_requires_admin(self, plan: ChangePlan) -> bool:
        return os.geteuid() != 0 and self._workflow_requires_admin(plan.workflow)

    def _order_batch_change_ids(self, change_ids: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        """Orden estable del lote con dependencias YAML antes de consumidores.

        Los DAG portables se mantienen como unidades aisladas y conservan el
        orden elegido por la persona. Para YAML, si la dependencia también fue
        seleccionada explícitamente, se adelanta antes de su consumidor.
        """
        requested: list[str] = []
        for raw in change_ids:
            change_id = str(raw)
            self._require_change(change_id)
            if change_id not in requested:
                requested.append(change_id)
        if not requested:
            raise ValueError("Selecciona al menos un cambio para integrar.")

        selected = set(requested)
        ordered: list[str] = []
        visiting: set[str] = set()

        def visit(change_id: str) -> None:
            if change_id in ordered:
                return
            if change_id in visiting:
                raise ValueError(f"Ciclo de dependencias al preparar el lote: {change_id}.")
            visiting.add(change_id)
            declarative = self._declarative_changes.get(change_id)
            if declarative is not None:
                for dependency in declarative.requires_changes:
                    if dependency in selected:
                        visit(dependency)
            visiting.remove(change_id)
            ordered.append(change_id)

        for change_id in requested:
            visit(change_id)
        return tuple(ordered)

    def build_batch_plan(self, change_ids: tuple[str, ...] | list[str]) -> ChangeBatchPlan:
        """Prepara una revisión única sin fusionar ni reescribir los DAG.

        La ejecución posterior reconstruye cada plan justo antes de correrlo.
        Esa reconciliación tardía es deliberada: el cambio N puede satisfacer
        una capacidad que el cambio N+1 necesita y así evita repetir trabajo.
        """
        ordered = self._order_batch_change_ids(change_ids)
        preview_plans: list[ChangePlan] = []
        scheduled: set[str] = set()
        for change_id in ordered:
            plan = self.build_plan(change_id)
            declarative = self._declarative_changes.get(change_id)
            if declarative is not None:
                prior_dependencies = [
                    dependency
                    for dependency in dependency_order(change_id, self._declarative_changes)[:-1]
                    if dependency in scheduled
                ]
                if prior_dependencies:
                    prefixes = tuple(f"yaml.{dependency}." for dependency in prior_dependencies)
                    visible_phases = tuple(
                        phase for phase in plan.phases
                        if not phase.step_id.startswith(prefixes)
                    )
                    dependency_names = [
                        self._declarative_changes[item].recipe.name for item in prior_dependencies
                    ]
                    reuse_notice = (
                        "Dependencia ya programada antes en este lote: "
                        + ", ".join(dependency_names)
                        + ". Al llegar aquí Styler reconstruirá el plan y reutilizará el estado comprobado."
                    )
                    plan = replace(
                        plan,
                        phases=visible_phases,
                        notice=self._join_notice(plan.notice, reuse_notice),
                    )
            preview_plans.append(plan)
            scheduled.add(change_id)
        plans = tuple(preview_plans)
        names = [plan.name for plan in plans]
        summary = (
            f"Styler integrará {len(plans)} cambio(s) de forma secuencial: "
            + " → ".join(names)
            + ". Cada DAG conserva su identidad y se reconciliará de nuevo justo antes de ejecutarse."
        )
        notice = (
            "Si un cambio falla, el lote se detiene antes de iniciar los siguientes. "
            "Los cambios ya completados conservan sus recibos y pueden retirarse individualmente."
        )
        return ChangeBatchPlan(
            change_ids=ordered,
            plans=plans,
            name=f"Lote de {len(plans)} cambios",
            summary=summary,
            notice=notice,
            operation="apply",
        )

    def batch_requires_admin(self, batch: ChangeBatchPlan) -> bool:
        return any(self.plan_requires_admin(plan) for plan in batch.plans)

    def execute_batch(
        self,
        change_ids: tuple[str, ...] | list[str],
        progress: BatchProgressCallback = None,
    ) -> ChangeBatchExecutionResult:
        """Ejecuta los cambios uno por uno usando la ruta normal de ``execute``.

        No existe un segundo ejecutor para lotes. Cada elemento vuelve a pasar
        por ``build_plan``/PipeCraft/recibos. Esto impide colisiones de IDs entre
        DAG importados y permite que el sistema observado tras un cambio sea la
        entrada real del siguiente.
        """
        ordered = self._order_batch_change_ids(change_ids)
        preview_names = {plan.change_id: plan.name for plan in self.build_batch_plan(ordered).plans}
        results: list[ChangeExecutionResult] = []
        skipped_ids: tuple[str, ...] = ()

        for index, change_id in enumerate(ordered, 1):
            def _nested(event: ChangeProgressEvent, *, current_index: int = index) -> None:
                if progress is None:
                    return
                total = ((current_index - 1) + event.total_progress) / len(ordered)
                progress(
                    ChangeBatchProgressEvent(
                        change_id=event.change_id,
                        change_name=event.change_name,
                        change_index=current_index,
                        change_count=len(ordered),
                        change_progress=event.total_progress,
                        total_progress=max(0.0, min(1.0, total)),
                        phase_label=event.phase_label,
                        operation=event.operation,
                        status=event.status,
                        message=event.message,
                        event_type=event.event_type,
                        terminal_line=event.terminal_line,
                        command=event.command,
                        pid=event.pid,
                        elapsed_seconds=event.elapsed_seconds,
                        quiet_seconds=event.quiet_seconds,
                        log_path=event.log_path,
                        returncode=event.returncode,
                    )
                )

            # ``execute`` reconstruye el plan aquí; no reutilizamos el preview.
            result = self.execute(change_id, progress=_nested)
            results.append(result)
            if progress is not None:
                progress(
                    ChangeBatchProgressEvent(
                        change_id=result.change_id,
                        change_name=result.name,
                        change_index=index,
                        change_count=len(ordered),
                        change_progress=1.0 if result.ok else 0.0,
                        total_progress=(index / len(ordered)) if result.ok else ((index - 1) / len(ordered)),
                        phase_label="Cambio completado" if result.ok else "Cambio detenido",
                        operation=result.title,
                        status="completed" if result.ok else "failed",
                        message=result.message,
                    )
                )
            if not result.ok:
                skipped_ids = ordered[index:]
                break

        skipped_names = tuple(preview_names.get(item, _change_name(item)) for item in skipped_ids)
        failed = next((result for result in results if not result.ok), None)
        ok = failed is None and len(results) == len(ordered)
        if ok:
            title = f"Se integraron {len(results)} cambios"
            message = "El lote terminó completo. Cada cambio conservó sus recibos y su estado independiente."
        else:
            completed = sum(1 for result in results if result.ok)
            title = "El lote se detuvo por un fallo"
            if failed is not None:
                message = (
                    f"{completed} cambio(s) se completaron antes del fallo en {failed.name}. "
                    f"{len(skipped_ids)} cambio(s) quedaron sin iniciar."
                )
            else:
                message = "El lote no pudo completarse."
        return ChangeBatchExecutionResult(
            change_ids=ordered,
            results=tuple(results),
            skipped_ids=skipped_ids,
            skipped_names=skipped_names,
            ok=ok,
            title=title,
            message=message,
        )

    def execute(
        self,
        change_id: str,
        provider_id: str | None = None,
        progress: ProgressCallback = None,
        options: dict[str, Any] | None = None,
    ) -> ChangeExecutionResult:
        plan = self.build_plan(change_id, provider_id, options)
        portable_source = self._portable_source(change_id)
        try:
            self._assert_execution_storage_writable(change_id)
            self._save_record(
                change_id,
                {
                    "status": ChangeStatus.INTEGRATING,
                    "name": plan.name,
                    "provider_id": plan.provider_id,
                    "provider_label": plan.provider_label,
                    "automation_level": plan.automation_level,
                    "required_packages": self._required_packages(plan.workflow),
                    "message": (
                        "Continuando la integración desde los pasos pendientes."
                        if plan.continuation_mode
                        else "Integración en curso."
                    ),
                    "attempt_mode": "continue" if plan.continuation_mode else "fresh",
                },
            )
        except ChangeStateWriteError as exc:
            # No se permite empezar un DAG si Styler ya sabe que no podrá
            # registrar su estado. Esto evita producir efectos huérfanos.
            return self._state_write_failure_result(
                plan=plan, exc=exc, after_effects=False
            )

        phase_by_step = {phase.step_id: (index, phase) for index, phase in enumerate(plan.phases, 1)}
        runtime_started = False

        def runtime_progress(raw: dict[str, Any]) -> None:
            nonlocal runtime_started
            runtime_started = True
            if progress is None:
                return
            step_id = str(raw.get("step_id", ""))
            index, phase = phase_by_step.get(
                step_id,
                (1, ChangePhase(step_id, str(raw.get("phase_label", "Procesando")), "", 1, step_id)),
            )
            progress(
                ChangeProgressEvent(
                    change_id=change_id,
                    change_name=plan.name,
                    phase_id=phase.phase_id,
                    phase_label=phase.label,
                    operation=str(raw.get("operation") or raw.get("message") or phase.description),
                    phase_index=index,
                    phase_count=len(plan.phases),
                    phase_progress=raw.get("phase_progress"),
                    total_progress=max(0.0, min(1.0, float(raw.get("total_progress", 0.0)))),
                    status=str(raw.get("status", "running")),
                    message=str(raw.get("message", "")),
                    event_type=str(raw.get("event_type", "progress")),
                    terminal_line=str(raw.get("terminal_line", "")),
                    command=str(raw.get("command", "")),
                    pid=(int(raw["pid"]) if raw.get("pid") is not None else None),
                    elapsed_seconds=float(raw.get("elapsed_seconds", 0.0) or 0.0),
                    quiet_seconds=float(raw.get("quiet_seconds", 0.0) or 0.0),
                    log_path=str(raw.get("log_path", "")),
                    returncode=(int(raw["returncode"]) if raw.get("returncode") is not None else None),
                )
            )

        def runtime_submitted(run_id: str) -> None:
            self._save_record(
                change_id,
                {
                    "status": ChangeStatus.INTEGRATING,
                    "pipecraft_run_id": run_id,
                    "runtime_backend": "pipecraft/1.5",
                    "message": "PipeCraft aceptó el DAG y lo está ejecutando.",
                },
            )

        context_values = {
            "home": str(self.home),
            "progress_callback": runtime_progress,
            "run_submitted_callback": runtime_submitted,
            # Habilita los recibos: sin change_id, los ejecutores no
            # registran nada y el cambio no sería reversible.
            "change_id": change_id,
            "options": dict(plan.options),
            "continuation_mode": plan.continuation_mode,
            "reconciled_steps": dict(plan.reconciled_steps),
        }
        if portable_source is not None:
            package, _graph = portable_source
            context_values.update(
                {
                    "package_content_root": Path(package.install_path) / "content",
                    "package_id": package.manifest.package_id,
                    "package_version": package.manifest.version,
                }
            )
        context = ExecutionContext(
            root=self.root,
            dry_run=False,
            approve=True,
            values=context_values,
        )
        sudo_ticket = None
        if self.plan_requires_admin(plan) and shutil.which("sudo"):
            sudo_ticket = keepalive_for(["sudo", "-n"])
            if sudo_ticket is not None and not sudo_ticket.ensure():
                message = (
                    "Styler necesita autorización administrativa antes de ejecutar este DAG. "
                    "No se inició ningún nodo privilegiado."
                )
                self._save_record(
                    change_id,
                    {
                        "status": ChangeStatus.FAILED,
                        "name": plan.name,
                        "provider_id": plan.provider_id,
                        "provider_label": plan.provider_label,
                        "automation_level": plan.automation_level,
                        "message": message,
                        "required_packages": self._required_packages(plan.workflow),
                        "options": dict(plan.options),
                        "attempt_mode": "continue" if plan.continuation_mode else "fresh",
                    },
                )
                return ChangeExecutionResult(
                    change_id=change_id, name=plan.name, ok=False, status=ChangeStatus.FAILED,
                    title=f"No se pudo iniciar {plan.name}", message=message,
                    provider_id=plan.provider_id, provider_label=plan.provider_label,
                    automation_level=plan.automation_level,
                    details=(
                        "✕ Falta una credencial sudo vigente.",
                        "El DAG se conservó intacto; vuelve a intentarlo y autoriza cuando Styler lo solicite.",
                    ),
                    options=dict(plan.options), operation=plan.operation,
                )
            if sudo_ticket is not None:
                sudo_ticket.start()
        try:
            try:
                run = WorkflowEngine(extended_registry(), backend="auto").run(plan.workflow, context)
            except OSError as exc:
                if not self._is_storage_failure(exc):
                    raise
                storage_exc = self._storage_error(
                    exc, self.root / ".styler" / "runs"
                )
                return self._state_write_failure_result(
                    plan=plan,
                    exc=storage_exc,
                    after_effects=runtime_started,
                    details=(
                        "PipeCraft ya había iniciado este cambio."
                        if runtime_started
                        else "PipeCraft todavía no había iniciado ningún nodo.",
                    ),
                )
        finally:
            if sudo_ticket is not None:
                sudo_ticket.stop()
        meaningful_results = [result for result in run.results if not result.step_id.startswith("__")]
        ok = bool(meaningful_results) and all(result.success for result in meaningful_results)
        handoff_path = ""
        instructions_path = ""
        for result in meaningful_results:
            handoff_path = str(result.data.get("handoff_path") or handoff_path)
            instructions_path = str(result.data.get("instructions_path") or instructions_path)

        if portable_source is not None and ok:
            status = ChangeStatus.INTEGRATED
            title = f"{plan.name} se integró correctamente"
            message = "El DAG del paquete terminó y Styler confirmó sus pasos requeridos."
        elif portable_source is not None:
            status = ChangeStatus.FAILED
            title = f"No se pudo completar {plan.name}"
            failed = next((item for item in meaningful_results if not item.success), None)
            message = failed.message if failed else "La ejecución terminó sin confirmar el resultado."
        elif ok and plan.automation_level == AutomationLevel.AUTOMATIC:
            status = ChangeStatus.INTEGRATED
            title = "PhotoGIMP se integró correctamente"
            message = "GIMP y PhotoGIMP quedaron instalados, adaptados y verificados."
        elif ok:
            status = ChangeStatus.PREPARED
            title = "PhotoGIMP quedó preparado"
            message = (
                "GIMP quedó instalado y PhotoGIMP fue descargado. La integración final "
                "debe completarse manualmente."
            )
        else:
            status = ChangeStatus.FAILED
            title = "No se pudo completar PhotoGIMP"
            failed = next((item for item in meaningful_results if not item.success), None)
            message = failed.message if failed else "La ejecución terminó sin confirmar el resultado."

        detail_lines: list[str] = []
        for item in meaningful_results:
            prefix = "✓ " if item.success else "✕ "
            detail_lines.append(prefix + item.message)
            if not item.success:
                error_code = str(item.data.get("error_code") or "")
                command = str(item.data.get("command") or "")
                artifact = str(item.data.get("artifact") or "")
                if error_code:
                    detail_lines.append(f"  Código técnico: {error_code}")
                if command:
                    detail_lines.append(f"  Comando: {command}")
                if artifact:
                    detail_lines.append(f"  Log: {artifact}")
        details = tuple(detail_lines)
        try:
            self._save_record(
                change_id,
                {
                    "status": status,
                    "name": plan.name,
                    "provider_id": plan.provider_id,
                    "provider_label": plan.provider_label,
                    "automation_level": plan.automation_level,
                    "message": message,
                    "report_path": run.report_path,
                    "handoff_path": handoff_path,
                    "instructions_path": instructions_path,
                    "reversible": self.can_rollback(change_id),
                    "required_packages": self._required_packages(plan.workflow),
                    "options": dict(plan.options),
                    "last_run_id": run.run_id,
                    "pipecraft_run_id": run.run_id if ".styler/pipecraft" in str(getattr(run, "run_dir", "")).replace("\\", "/") else "",
                    "runtime_backend": "pipecraft/1.5" if ".styler/pipecraft" in str(getattr(run, "run_dir", "")).replace("\\", "/") else "local-compat",
                    "reconciled_steps": dict(plan.reconciled_steps),
                    "attempt_mode": "continue" if plan.continuation_mode else "fresh",
                },
            )
        except ChangeStateWriteError as exc:
            return self._state_write_failure_result(
                plan=plan,
                exc=exc,
                after_effects=True,
                run_report=run.report_path,
                details=details,
            )
        return ChangeExecutionResult(
            change_id=change_id,
            name=plan.name,
            ok=ok,
            status=status,
            title=title,
            message=message,
            provider_id=plan.provider_id,
            provider_label=plan.provider_label,
            automation_level=plan.automation_level,
            report_path=run.report_path,
            handoff_path=handoff_path,
            instructions_path=instructions_path,
            details=details,
            options=dict(plan.options),
            diagnostic_path=run.report_path,
            operation=plan.operation,
        )

    # ----------------------------------------------------------------- opciones
    def _apply_options(
        self, workflow: WorkflowDefinition, options: dict[str, Any]
    ) -> WorkflowDefinition:
        """Las opciones entran al mismo compilador: incluyen o excluyen pasos."""
        if not options.get("backup", True):
            for step in [item for item in workflow.steps if item.step_type == "backup_config"]:
                workflow = drop_step(workflow, step.id)
        timeout = options.get("startup_timeout_seconds")
        for step in workflow.steps:
            if step.step_type == "initialize_flatpak_app" and timeout:
                step.config["startup_timeout_seconds"] = float(timeout)
            if step.step_type in {"install_overlay", "apply_config"}:
                step.config["rewrite_launchers"] = bool(options.get("rewrite_launchers", True))
        return workflow

    def _continuation_mode(self, change_id: str) -> bool:
        record = self._load_records().get(change_id, {})
        return isinstance(record, dict) and str(record.get("status") or "") in CONTINUATION_STATUSES

    @staticmethod
    def _join_notice(left: str, right: str) -> str:
        return " ".join(part.strip() for part in (left, right) if part and part.strip())

    @staticmethod
    def _decorate_reconciled_phases(
        phases: tuple[ChangePhase, ...],
        reconciled: dict[str, Any],
    ) -> tuple[ChangePhase, ...]:
        labels = {
            "app.gimp.install": "Reutilizando GIMP ya instalado",
            "app.gimp.resolve-facts": "Reutilizando la versión detectada de GIMP",
            "app.gimp.initialize": "Reutilizando la configuración inicializada de GIMP",
            "app.photogimp.backup": "Reutilizando el respaldo anterior",
            "app.photogimp.install": "PhotoGIMP ya copiado; continuar con verificación",
            "app.photogimp.launch": "Reutilizando el arranque ya confirmado de GIMP",
        }
        decorated: list[ChangePhase] = []
        for phase in phases:
            result = reconciled.get(phase.step_id)
            if result is None:
                decorated.append(phase)
                continue
            decorated.append(
                ChangePhase(
                    phase_id=phase.phase_id,
                    label=labels.get(phase.step_id, f"Ya completado · {phase.label}"),
                    description=str(result.message),
                    weight=phase.weight,
                    step_id=phase.step_id,
                    determinate=False,
                )
            )
        return tuple(decorated)

    # ---------------------------------------------------------------- reversión
    def journal_for_change(self, change_id: str) -> ReceiptJournal:
        self._validate_change_id(change_id)
        return ReceiptJournal(self.root, change_id)

    def checkpoint_for_change(self, change_id: str) -> dict[str, Any] | None:
        """Último checkpoint vivo de un cambio."""
        for receipt in reversed(self.journal_for_change(change_id).pending_undo()):
            if receipt.kind == ReceiptKind.CHECKPOINT_CREATED:
                return {"receipt_id": receipt.receipt_id, **dict(receipt.data)}
        return None

    def _all_checkpoint_receipts(self) -> list[StepReceipt]:
        return all_checkpoint_receipts(self.root)

    def system_checkpoints(self) -> tuple[dict[str, Any], ...]:
        """Últimos cinco checkpoints automáticos administrados por Styler."""
        newest = sorted(
            self._all_checkpoint_receipts(), key=lambda item: item.created_at, reverse=True
        )[:5]
        return tuple(
            {
                "receipt_id": item.receipt_id,
                "created_at": item.created_at,
                **dict(item.data),
            }
            for item in newest
        )

    def prune_system_checkpoints(self, keep: int = 5) -> tuple[str, ...]:
        """Poda checkpoints vivos más allá de los ``keep`` más recientes.

        Un checkpoint nunca se poda si su cambio todavía tiene recibos
        pendientes de reversión: la promesa de Deshacer no puede depender de
        un respaldo que ya fue borrado por retención.
        """
        return prune_system_checkpoints(self.root, keep=keep)

    def restore_checkpoint(
        self,
        checkpoint_id: str,
        progress: ProgressCallback = None,
        *,
        dry_run: bool = False,
    ) -> ChangeExecutionResult:
        for checkpoint in self.system_checkpoints():
            if str(checkpoint.get("checkpoint_id") or "") == checkpoint_id:
                change_id = str(checkpoint.get("change_id") or "")
                if change_id:
                    return self.rollback_change(change_id, progress=progress, dry_run=dry_run)
        return ChangeExecutionResult(
            change_id="",
            name="Checkpoint",
            ok=False,
            status=ChangeStatus.UNKNOWN,
            title="Checkpoint no encontrado",
            message=f"Styler no encontró un checkpoint vivo con ID {checkpoint_id}.",
        )

    def can_rollback(self, change_id: str) -> bool:
        """Hay algo que deshacer solo si quedaron recibos vivos.

        No se promete reversibilidad por el hecho de que el cambio sea
        automático: se promete porque existen efectos registrados.
        """
        try:
            return bool(self.journal_for_change(change_id).pending_undo())
        except (OSError, ValueError):
            return False

    def rollback_plan(self, change_id: str) -> WorkflowDefinition:
        receipts = self.journal_for_change(change_id).pending_undo()
        workflow = compile_rollback_workflow(
            receipts,
            name=f"undo-{change_id}",
            description=f"Reversión de {change_id} compilada desde {len(receipts)} recibo(s).",
            package_protections=self._package_protections(excluding_change_id=change_id),
        )
        workflow.metadata.update({"change_id": change_id, "dag_role": "undo"})
        return annotate_workflow_methods(
            workflow,
            registry=self._method_registry,
            context=MethodContext.detect(),
            policy=self._method_policy,
        )

    def _with_initial_checkpoint(
        self,
        workflow: WorkflowDefinition,
        change_id: str,
    ) -> WorkflowDefinition:
        paths: list[str] = []
        packages: list[dict[str, str]] = []
        effectful_types = {
            "install_package",
            "initialize_flatpak_app",
            "backup_config",
            "install_overlay",
            "apply_config",
            "prepare_manual_handoff",
            "install_package_artifact",
            "integrate_appimage",
        }
        for step in workflow.steps:
            if step.step_type == "install_package":
                package = dict(step.config.get("package") or {})
                manager = str(package.get("manager") or "")
                name = str(package.get("name") or "")
                if manager and name:
                    packages.append({"manager": manager, "name": name})
            for key in ("backup_source", "target", "config_root"):
                value = str(step.config.get(key) or "")
                if value and value not in paths:
                    paths.append(value)

        checkpoint = StepDefinition(
            id="change.checkpoint",
            step_type="create_change_checkpoint",
            description="Crear punto de retorno inicial",
            needs=[],
            phase="checkpoint",
            block=change_id,
            tags=["checkpoint", "reversible"],
            barrier=True,
            config={
                "checkpoint_id": f"{change_id}-initial",
                "scope": "change",
                "paths": paths,
                "packages": packages,
            },
        )
        steps: list[StepDefinition] = [checkpoint]
        for step in workflow.steps:
            if step.step_type in effectful_types and checkpoint.id not in step.needs:
                step.needs = [checkpoint.id, *step.needs]
            steps.append(step)
        phases = dict(workflow.phases)
        phases.setdefault("checkpoint", PhaseDefinition("Crear punto de retorno"))
        return WorkflowDefinition(
            name=workflow.name,
            steps=steps,
            description=workflow.description,
            operation=workflow.operation,
            metadata=dict(workflow.metadata),
            on_error=workflow.on_error,
            phases=phases,
            hooks=workflow.hooks,
            dependency_mode=workflow.dependency_mode,
            observations=dict(workflow.observations),
            outputs=dict(workflow.outputs),
            schema_version=workflow.schema_version,
        )

    def workflow_pair(
        self,
        change_id: str,
        provider_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ChangeWorkflowPair:
        """Devuelve los dos DAG sin fingir que uno es el otro al revés."""
        plan = self.build_plan(change_id, provider_id, options)
        undo = plan.undo_workflow
        return ChangeWorkflowPair(
            change_id=change_id,
            apply=plan.workflow,
            undo=undo,
            undo_available=undo is not None and self.can_rollback(change_id),
            undo_source="receipts",
        )

    def rollback_change(
        self,
        change_id: str,
        progress: ProgressCallback = None,
        *,
        dry_run: bool = False,
    ) -> ChangeExecutionResult:
        """Deshace efectos confirmados y declara con honestidad lo pendiente."""
        self._validate_change_id(change_id)
        journal = self.journal_for_change(change_id)
        receipts = journal.pending_undo()
        record = self._load_records().get(change_id, {})
        provider_id = str(record.get("provider_id") or "recorded")
        provider_label = str(
            record.get("provider_label") or PROVIDER_LABELS.get(provider_id, "Registrado por Styler")
        )
        name = str(record.get("name") or _change_name(change_id))

        if not receipts:
            return ChangeExecutionResult(
                change_id=change_id,
                name=name,
                ok=False,
                status=str(record.get("status") or ChangeStatus.UNKNOWN),
                title="No hay nada que deshacer",
                message=(
                    "Styler no tiene efectos registrados de este cambio en este equipo. "
                    "Puede que se aplicara antes de que existieran los recibos, o desde otra herramienta."
                ),
                provider_id=provider_id,
                provider_label=provider_label,
                automation_level=str(record.get("automation_level") or AutomationLevel.AUTOMATIC),
                operation="remove",
            )

        removal_plan = self.build_removal_plan(change_id)
        if not dry_run:
            try:
                self._assert_execution_storage_writable(change_id)
            except ChangeStateWriteError as exc:
                return self._state_write_failure_result(
                    plan=removal_plan, exc=exc, after_effects=False,
                    details=(
                        "El retiro no se inició: Styler necesita poder actualizar los recibos "
                        "mientras deshace cada efecto.",
                    ),
                )

        workflow = removal_plan.workflow
        workflow.description = f"Reversión de {name}."
        workflow.metadata.update({
            "max_workers": 4,
            "change_id": change_id,
            "change_name": name,
            "dag_role": "undo",
        })

        if not dry_run:
            try:
                self._save_record(
                    change_id, {**record, "status": ChangeStatus.REVERTING, "message": "Reversión en curso."}
                )
            except ChangeStateWriteError as exc:
                return self._state_write_failure_result(
                    plan=removal_plan, exc=exc, after_effects=False,
                    details=("No se inició ningún nodo de reversión.",),
                )

        phases = self._phases_for_removal(workflow)
        workflow.metadata.update(
            {
                "progress_weights": {phase.step_id: phase.weight for phase in phases},
                "progress_labels": {phase.step_id: phase.label for phase in phases},
            }
        )
        phase_by_step = {phase.step_id: (index, phase) for index, phase in enumerate(phases, 1)}
        rollback_started = False

        def runtime_progress(raw: dict[str, Any]) -> None:
            nonlocal rollback_started
            rollback_started = True
            if progress is None:
                return
            step_id = str(raw.get("step_id", ""))
            index, phase = phase_by_step.get(
                step_id, (1, ChangePhase(step_id, "Deshaciendo", "", 1, step_id))
            )
            progress(
                ChangeProgressEvent(
                    change_id=change_id,
                    change_name=name,
                    phase_id=phase.phase_id,
                    phase_label=phase.label,
                    operation=str(raw.get("operation") or phase.description),
                    phase_index=index,
                    phase_count=len(phases) or 1,
                    phase_progress=raw.get("phase_progress"),
                    total_progress=max(0.0, min(1.0, float(raw.get("total_progress", 0.0)))),
                    status=str(raw.get("status", "running")),
                    message=str(raw.get("message", "")),
                    event_type=str(raw.get("event_type", "progress")),
                    terminal_line=str(raw.get("terminal_line", "")),
                    command=str(raw.get("command", "")),
                    pid=(int(raw["pid"]) if raw.get("pid") is not None else None),
                    elapsed_seconds=float(raw.get("elapsed_seconds", 0.0) or 0.0),
                    quiet_seconds=float(raw.get("quiet_seconds", 0.0) or 0.0),
                    log_path=str(raw.get("log_path", "")),
                    returncode=(int(raw["returncode"]) if raw.get("returncode") is not None else None),
                )
            )

        context = ExecutionContext(
            root=self.root,
            dry_run=dry_run,
            approve=True,
            values={
                "home": str(self.home),
                "progress_callback": runtime_progress,
                "change_id": change_id,
            },
        )
        try:
            run = WorkflowEngine(extended_registry(), backend="auto").run(workflow, context)
        except OSError as exc:
            if not self._is_storage_failure(exc):
                raise
            storage_exc = self._storage_error(exc, self.root / ".styler" / "runs")
            return self._state_write_failure_result(
                plan=removal_plan,
                exc=storage_exc,
                after_effects=rollback_started,
                details=(
                    "La reversión ya había comenzado; Styler detuvo los pasos restantes para "
                    "no perder la correspondencia entre efectos y recibos."
                    if rollback_started
                    else "No se ejecutó ningún nodo de reversión.",
                ),
            )
        results = [item for item in run.results if not item.step_id.startswith("__")]
        step_by_id = {step.id: step for step in workflow.steps}
        hard_failures = [item for item in results if not item.success]
        incomplete = [
            item for item in results
            if item.success and item.data.get("fully_reverted") is False
        ]

        fully_reverted_ids: set[str] = set()
        for result in results:
            step = step_by_id.get(result.step_id)
            if not step or not result.success or result.data.get("fully_reverted") is not True:
                continue
            receipt_id = str(step.config.get("receipt_id") or "")
            if receipt_id:
                fully_reverted_ids.add(receipt_id)

        if not dry_run and fully_reverted_ids:
            try:
                journal.mark_rolled_back(
                    [item for item in receipts if item.receipt_id in fully_reverted_ids],
                    run_id=run.run_id,
                )
            except OSError as exc:
                storage_exc = self._storage_error(exc, journal.path)
                return self._state_write_failure_result(
                    plan=removal_plan, exc=storage_exc, after_effects=True,
                    run_report=run.report_path,
                    details=tuple(
                        ("✓ " if item.success else "✕ ") + item.message for item in results
                    ) + (
                        "Los efectos ya revertidos no pudieron marcarse como retirados en el diario.",
                    ),
                )

        remaining = journal.pending_undo() if not dry_run else receipts
        full = bool(results) and not hard_failures and not incomplete and not remaining
        partial = bool(results) and not hard_failures and not full
        ok = full

        if full:
            status = ChangeStatus.REVERTED
            title = "El cambio se deshizo"
            message = "Todos los efectos registrados fueron revertidos y verificados."
        elif partial:
            status = ChangeStatus.PARTIALLY_REVERTED
            title = "La reversión necesita una decisión"
            pending_packages = [
                item for item in remaining if item.kind == ReceiptKind.PACKAGE_INSTALLED
            ]
            if pending_packages:
                message = (
                    "La configuración de PhotoGIMP fue revertida, pero uno o más paquetes "
                    "no pudieron desinstalarse o siguen protegidos por otros cambios activos."
                )
            else:
                message = (
                    "Styler revirtió los efectos que pudo demostrar, pero conservó elementos "
                    "sin respaldo o con contenido ajeno. No afirmó que el equipo volviera por completo."
                )
        else:
            status = ChangeStatus.NEEDS_ATTENTION
            title = "La reversión quedó incompleta"
            message = next(
                (item.message for item in hard_failures),
                "La reversión terminó sin confirmar el resultado.",
            )

        if not dry_run:
            try:
                self._save_record(
                    change_id,
                    {
                        "status": status,
                        "provider_id": provider_id,
                        "provider_label": provider_label,
                        "automation_level": record.get("automation_level", AutomationLevel.AUTOMATIC),
                        "message": message,
                        "report_path": run.report_path,
                        "reversible": bool(remaining),
                        "pending_receipts": len(remaining),
                    },
                )
            except ChangeStateWriteError as exc:
                return self._state_write_failure_result(
                    plan=removal_plan, exc=exc, after_effects=True,
                    run_report=run.report_path,
                    details=tuple(
                        ("✓ " if item.success else "✕ ") + item.message for item in results
                    ),
                )

        details: list[str] = []
        for item in results:
            if not item.success:
                prefix = "✕ "
            elif item.data.get("fully_reverted") is False:
                prefix = "⚠ "
            else:
                prefix = "✓ "
            details.append(prefix + item.message)

        return ChangeExecutionResult(
            change_id=change_id,
            name=name,
            ok=ok,
            status=status,
            title=title,
            message=message,
            provider_id=provider_id,
            provider_label=provider_label,
            automation_level=str(record.get("automation_level") or AutomationLevel.AUTOMATIC),
            report_path=run.report_path,
            details=tuple(details),
            operation="remove",
        )

    # --------------------------------------------------------------- plan build
    def _build_automatic_photogimp(self, provider_id: str) -> WorkflowDefinition:
        resolution = resolve(
            self._registry,
            ["app.photogimp"],
            family=self._target.family or "*",
            preferred_providers={"app.gimp": provider_id},
        )
        compiled = compile_workflow(self._registry, resolution, name="integrate-photogimp")
        if not compiled.ok:
            raise ValueError("; ".join(issue.message for issue in compiled.errors))
        return self._with_final_launch(compiled.workflow)

    @staticmethod
    def _with_final_launch(workflow: WorkflowDefinition) -> WorkflowDefinition:
        """Añade el último paso del instructivo oficial: «Open GIMP».

        Comprobar el marcador y el manifiesto demuestra que los archivos están
        donde deben, no que GIMP arranque con ellos. Una configuración copiada
        con precisión puede seguir impidiendo el arranque, así que el cambio no
        se declara integrado hasta que GIMP se abrió con PhotoGIMP aplicado.
        """
        initialize = next(
            (step for step in workflow.steps if step.id == "app.gimp.initialize"),
            None,
        )
        verify = next(
            (step for step in workflow.steps if step.id == "app.photogimp.verify"),
            None,
        )
        if initialize is None or verify is None:
            return workflow

        config = dict(initialize.config)
        config["semantic_operations"] = [
            {"operation": "application.launch", "label": "Abrir GIMP ya adaptado por PhotoGIMP"},
            {"operation": "wait.observable", "label": "Esperar una ventana estable con la nueva configuración"},
            {"operation": "application.stop", "label": "Cerrar GIMP tras confirmar el arranque"},
            {"operation": "wait.observable", "label": "Esperar el cierre y que la configuración deje de cambiar"},
        ]
        launch_step = StepDefinition(
            id="app.photogimp.launch",
            step_type="initialize_flatpak_app",
            description="Abrir GIMP para confirmar que arranca con PhotoGIMP aplicado",
            needs=[verify.id],
            phase="verify",
            block="app.photogimp",
            tags=["application_overlay", "verification"],
            required=True,
            provider=initialize.provider,
            timeout=initialize.timeout,
            exclusive_resources=["user-config:gimp"],
            config=config,
        )
        return WorkflowDefinition(
            name=workflow.name,
            description=workflow.description,
            steps=[*workflow.steps, launch_step],
            metadata=dict(workflow.metadata),
        )

    def _build_assisted_photogimp(self, provider_id: str) -> WorkflowDefinition:
        resolution = resolve(
            self._registry,
            ["app.gimp"],
            family=self._target.family or "*",
            preferred_providers={"app.gimp": provider_id},
        )
        compiled = compile_workflow(self._registry, resolution, name="prepare-photogimp-manual")
        if not compiled.ok:
            raise ValueError("; ".join(issue.message for issue in compiled.errors))
        photogimp = self._registry.get("app.photogimp")
        source = ""
        if photogimp is not None:
            source = next((provider.source for provider in photogimp.providers if provider.source), "")
        if not source.startswith(PHOTOGIMP_RELEASE_PREFIX):
            raise ValueError("El catálogo no contiene una fuente oficial válida de PhotoGIMP.")
        steps = list(compiled.workflow.steps)
        steps.append(
            StepDefinition(
                id="app.photogimp.handoff",
                step_type="prepare_manual_handoff",
                description="Descargar PhotoGIMP y preparar la integración manual",
                needs=["app.gimp.verify"],
                phase="handoff",
                provider=provider_id,
                shared_resources=["network"],
                config={
                    "source": source,
                    "change_name": "PhotoGIMP",
                    "provider_label": PROVIDER_LABELS.get(provider_id, provider_id),
                },
            )
        )
        return WorkflowDefinition(
            name=compiled.workflow.name,
            description="Preparación asistida de PhotoGIMP",
            steps=steps,
            metadata=dict(compiled.workflow.metadata),
        )

    def _phases_for_workflow(self, workflow: WorkflowDefinition, automatic: bool) -> tuple[ChangePhase, ...]:
        automatic_meta = {
            "change.checkpoint": ("Creando punto de retorno", "Guardando el estado inicial reversible.", 5),
            "app.gimp.install": ("Instalando GIMP", "Descargando e instalando GIMP desde Flathub.", 23),
            "app.gimp.resolve-facts": ("Detectando versión de GIMP", "Consultando versión, rama y carpeta de configuración real.", 4),
            "app.gimp.initialize": ("Iniciando GIMP", "Abriendo GIMP para crear la configuración de la versión detectada.", 10),
            "app.gimp.verify": ("Verificando GIMP", "Comprobando que GIMP quedó disponible.", 5),
            "app.photogimp.backup": ("Protegiendo tu configuración", "Creando un respaldo antes de modificar GIMP.", 10),
            "app.photogimp.install": ("Integrando PhotoGIMP", "Descargando, adaptando y copiando PhotoGIMP.", 35),
            "app.photogimp.verify": ("Verificando PhotoGIMP", "Comprobando el marcador y la carpeta destino.", 15),
            "app.photogimp.launch": (
                "Abriendo GIMP con PhotoGIMP",
                "Confirmando que GIMP arranca con la nueva configuración.",
                12,
            ),
        }
        assisted_meta = {
            "change.checkpoint": ("Creando punto de retorno", "Guardando el estado inicial reversible.", 5),
            "app.gimp.install": ("Instalando GIMP", "Instalando GIMP desde la fuente elegida.", 45),
            "app.gimp.verify": ("Verificando GIMP", "Comprobando que GIMP quedó disponible.", 10),
            "app.photogimp.handoff": ("Preparando PhotoGIMP", "Descargando el archivo y creando instrucciones.", 45),
        }
        metadata = automatic_meta if automatic else assisted_meta
        phases: list[ChangePhase] = []
        by_id = {step.id: step for step in workflow.steps}
        for step_id in topological_order(workflow.steps):
            step = by_id[step_id]
            label, description, weight = metadata.get(
                step.id,
                (step.description or step.id, step.description, 5),
            )
            phases.append(
                ChangePhase(
                    phase_id=step.id.replace(".", "-"),
                    label=label,
                    description=description,
                    weight=float(weight),
                    step_id=step.id,
                    determinate=step.step_type in {"initialize_flatpak_app", "prepare_manual_handoff"},
                )
            )
        total = sum(item.weight for item in phases) or 1.0
        return tuple(
            ChangePhase(
                phase_id=item.phase_id,
                label=item.label,
                description=item.description,
                weight=item.weight / total,
                step_id=item.step_id,
                determinate=item.determinate,
            )
            for item in phases
        )

    def _phases_for_removal(self, workflow: WorkflowDefinition) -> tuple[ChangePhase, ...]:
        """Convierte el Undo DAG en pasos concretos y legibles."""
        by_id = {step.id: step for step in workflow.steps}
        phases: list[ChangePhase] = []
        for step_id in topological_order(workflow.steps):
            step = by_id[step_id]
            config = step.config
            if step.step_type == "undo_remove_paths":
                created = config.get("created_paths") or []
                overwritten = config.get("overwritten") or []
                directories = config.get("created_directories") or []
                label = "Retirando archivos del cambio"
                description = (
                    f"Eliminar {len(created)} archivo(s) creado(s), restaurar "
                    f"{len(overwritten)} archivo(s) sustituido(s) y retirar "
                    f"{len(directories)} directorio(s) que queden vacíos."
                )
                weight = 30.0
            elif step.step_type == "undo_restore_backup":
                source = str(config.get("source") or "la configuración anterior")
                label = "Restaurando configuración original"
                description = f"Restaurar {source} desde el respaldo completo registrado."
                weight = 25.0
            elif step.step_type == "undo_restore_checkpoint":
                paths = config.get("paths") or []
                label = "Restableciendo el estado inicial"
                description = (
                    f"Comparar y restaurar {len(paths)} ruta(s) según el checkpoint "
                    "creado antes de integrar el cambio."
                )
                weight = 20.0
            elif step.step_type == "uninstall_package":
                package = str(config.get("package") or "la aplicación")
                if bool(config.get("was_present", False)):
                    label = f"Conservando {package}"
                    description = (
                        f"{package} ya existía antes del cambio; Styler no lo desinstalará."
                    )
                elif config.get("protected_by_changes"):
                    protected = ", ".join(str(x) for x in config["protected_by_changes"])
                    label = f"Conservando {package}"
                    description = (
                        f"{package} se conservará porque también lo necesita: {protected}."
                    )
                else:
                    label = f"Desinstalando {package}"
                    description = (
                        f"Desinstalar {package} con {config.get('manager', 'su gestor')} "
                        "porque Styler registró que no existía antes."
                    )
                weight = 20.0
            else:
                label = step.description or "Revisando efecto registrado"
                description = step.description or step.id
                weight = 5.0
            phases.append(
                ChangePhase(
                    phase_id=step.id.replace(".", "-"),
                    label=label,
                    description=description,
                    weight=weight,
                    step_id=step.id,
                    determinate=False,
                )
            )
        total = sum(item.weight for item in phases) or 1.0
        return tuple(
            ChangePhase(
                phase_id=item.phase_id,
                label=item.label,
                description=item.description,
                weight=item.weight / total,
                step_id=item.step_id,
                determinate=item.determinate,
            )
            for item in phases
        )

    @staticmethod
    def _removal_summary(workflow: WorkflowDefinition) -> tuple[str, str]:
        remove_steps = [s for s in workflow.steps if s.step_type == "undo_remove_paths"]
        backups = [s for s in workflow.steps if s.step_type in {"undo_restore_backup", "undo_restore_checkpoint"}]
        packages = [s for s in workflow.steps if s.step_type == "uninstall_package"]
        files_created = sum(len(s.config.get("created_paths") or []) for s in remove_steps)
        files_overwritten = sum(len(s.config.get("overwritten") or []) for s in remove_steps)
        uninstall = [
            str(s.config.get("package") or "")
            for s in packages
            if not s.config.get("was_present") and not s.config.get("protected_by_changes")
        ]
        retained = [
            str(s.config.get("package") or "")
            for s in packages
            if s.config.get("was_present") or s.config.get("protected_by_changes")
        ]
        summary = (
            f"Styler quitará {files_created} archivo(s) creado(s), restaurará "
            f"{files_overwritten} archivo(s) sustituido(s) y aplicará "
            f"{len(backups)} respaldo(s) o checkpoint(s)."
        )
        if uninstall:
            summary += " También desinstalará: " + ", ".join(uninstall) + "."
        if retained:
            summary += " Se conservará: " + ", ".join(retained) + "."
        notice = (
            "El retiro se calcula desde efectos comprobados. Styler no eliminará "
            "archivos ajenos, directorios con contenido posterior ni aplicaciones que "
            "ya existían o que otro cambio todavía necesita."
        )
        return summary, notice

    @staticmethod
    def _required_packages(workflow: WorkflowDefinition) -> list[dict[str, str]]:
        """Paquetes que el cambio necesita, aunque ya estuvieran instalados."""
        required: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for step in workflow.steps:
            if step.step_type == "install_package":
                package = dict(step.config.get("package") or {})
                manager = str(package.get("manager") or "")
                name = str(package.get("name") or "")
            elif step.step_type == "install_package_artifact":
                manager = str(step.config.get("manager") or "")
                name = str(step.config.get("package_name") or "")
            else:
                continue
            key = (manager, name)
            if not manager or not name or key in seen:
                continue
            seen.add(key)
            required.append({"manager": manager, "package": name})
        return required

    def _package_protections(self, *, excluding_change_id: str) -> dict[str, list[str]]:
        """Cambios activos que todavía declaran necesitar cada paquete."""
        protections: dict[str, list[str]] = {}
        for other_id, raw in self._load_records().items():
            if other_id == excluding_change_id or not isinstance(raw, dict):
                continue
            status = str(raw.get("status") or ChangeStatus.UNKNOWN)
            if status in {ChangeStatus.REVERTED, ChangeStatus.FAILED}:
                continue
            required = raw.get("required_packages") or []
            if not isinstance(required, list):
                continue
            for item in required:
                if not isinstance(item, dict):
                    continue
                manager = str(item.get("manager") or "")
                package = str(item.get("package") or "")
                if not manager or not package:
                    continue
                protections.setdefault(f"{manager}:{package}", []).append(str(other_id))
        return protections

    # ---------------------------------------------------------------- detection
    def _detect_photogimp(self) -> tuple[str, Path] | None:
        candidates = (
            ("flatpak", self.home / ".var" / "app" / "org.gimp.GIMP" / "config" / "GIMP"),
            ("snap", self.home / "snap" / "gimp" / "current" / ".config" / "GIMP"),
            ("native", self.home / ".config" / "GIMP"),
        )
        for provider, root in candidates:
            markers = [root / ".photogimp-marker"]
            if root.is_dir():
                markers.extend(
                    path / ".photogimp-marker"
                    for path in root.iterdir()
                    if path.is_dir() and _CONFIG_VERSION_DIR.fullmatch(path.name)
                )
            marker = next((path for path in markers if path.is_file()), None)
            if marker is not None:
                if provider == "native":
                    try:
                        metadata = dict(
                            line.split("=", 1)
                            for line in marker.read_text(encoding="utf-8").splitlines()
                            if "=" in line
                        )
                    except OSError:
                        metadata = {}
                    provider = str(metadata.get("provider") or self._target.native_manager or "native")
                return provider, marker
        return None

    # ------------------------------------------------------- source lifecycle
    def can_delete_available(self, change_id: str) -> bool:
        """Solo las fuentes propiedad del usuario se eliminan desde Cambios.

        Hoy esas fuentes son paquetes ``.stylerpkg`` importados o creados por
        el Constructor. Los cambios incorporados con Styler (PhotoGIMP) forman
        parte de la instalación y no se fingen como "ocultables".
        """
        self._validate_change_id(change_id)
        return self._portable_source(change_id) is not None

    def delete_available_change(self, change_id: str) -> str:
        """Elimina físicamente la fuente local de un cambio disponible.

        Si un paquete contiene varios DAG, todos desaparecen juntos porque la
        unidad almacenada es el ``.stylerpkg``. Los recibos de una integración
        previa se conservan: eliminar la fuente no equivale a retirar efectos.
        """
        self._validate_change_id(change_id)
        source = self._portable_source(change_id)
        if source is None:
            if change_id == "photogimp":
                raise ValueError(
                    "PhotoGIMP viene incorporado con Styler; no es un paquete local que pueda eliminarse."
                )
            raise ValueError(f"El cambio '{change_id}' no tiene una fuente local eliminable.")
        package, _graph = source
        package_id = package.manifest.package_id
        name = package.manifest.name or package_id
        self._portable_library.remove_all(package_id)
        return name

    # --------------------------------------------------------- YAML built-ins
    def _build_declarative_plan(self, change_id: str) -> ChangePlan:
        change = self._declarative_changes.get(change_id)
        if change is None:
            raise ValueError(f"El cambio YAML '{change_id}' no está disponible.")
        compatibility_error = change.compatibility_error(
            family=self._target.family, architecture=platform.machine(),
        )
        if compatibility_error:
            raise ValueError(compatibility_error)
        workflow = self._compose_declarative_workflow(change_id)
        continuation_mode = self._continuation_mode(change_id)
        reconciliation_context = ExecutionContext(
            root=self.root, dry_run=True, approve=False,
            values={"home": str(self.home), "change_id": change_id, "continuation_mode": continuation_mode},
        )
        reconciled_results = WorkflowEngine(extended_registry()).reconciliation(workflow, reconciliation_context)
        reconciled_steps = {
            step_id: {"status": result.status, "message": result.message, "data": dict(result.data)}
            for step_id, result in reconciled_results.items()
        }
        phases = self._decorate_reconciled_phases(
            self._phases_for_workflow(workflow, automatic=True), reconciled_results
        )
        weights = {phase.step_id: phase.weight for phase in phases}
        labels = {phase.step_id: phase.label for phase in phases}
        workflow.metadata.update({
            "max_workers": 2,
            "change_id": change_id,
            "change_name": change.recipe.name,
            "progress_weights": weights,
            "progress_labels": labels,
            "continuation_mode": continuation_mode,
            "reconciled_steps": reconciled_steps,
            "dag_role": "apply",
            "definition_source": "yaml",
            "definition_file": change.source.name,
        })
        workflow = annotate_workflow_methods(
            workflow, registry=self._method_registry,
            context=MethodContext.detect(), policy=self._method_policy,
        )
        requirements = ", ".join(change.requires_changes)
        notice = f"Definido declarativamente en {change.source.name}."
        if requirements:
            notice += f" Styler incluirá automáticamente: {requirements}."
        undo_workflow = self.rollback_plan(change_id) if self.can_rollback(change_id) else None
        return ChangePlan(
            change_id=change_id, name=change.recipe.name, provider_id="yaml",
            provider_label=change.provider_label, automation_level=AutomationLevel.AUTOMATIC,
            summary=change.description, notice=notice, phases=phases, workflow=workflow,
            undo_workflow=undo_workflow, options={}, reconciled_steps=reconciled_steps,
            continuation_mode=continuation_mode, operation="apply",
        )

    def _compose_declarative_workflow(self, change_id: str) -> WorkflowDefinition:
        """Compone YAML requeridos en un solo DAG sin crear una segunda ruta de ejecución."""
        order = dependency_order(change_id, self._declarative_changes)
        checkpoint = StepDefinition(
            id="change.checkpoint", step_type="create_change_checkpoint",
            description="Registrar el punto inicial antes de modificar el sistema.",
            phase="prepare", config={"scope": "yaml-change", "recipe_id": change_id},
            provides=["change.checkpoint.ready"],
        )
        steps: list[StepDefinition] = [checkpoint]
        previous_terminals = [checkpoint.id]
        phases: dict[str, PhaseDefinition] = {"prepare": PhaseDefinition(description="Preparación y checkpoint")}
        for recipe_id in order:
            recipe = self._declarative_changes[recipe_id].recipe
            compiled = compile_recipe(recipe)
            original = [step for step in compiled.steps if step.id != "change.checkpoint"]
            mapping = {step.id: f"yaml.{recipe_id}.{step.id}" for step in original}
            local_ids = set(mapping)
            depended_on: set[str] = set()
            namespaced: list[StepDefinition] = []
            for step in original:
                needs: list[str] = []
                for need in step.needs:
                    if need == "change.checkpoint":
                        needs.extend(previous_terminals)
                    elif need in mapping:
                        needs.append(mapping[need])
                    else:
                        needs.append(need)
                    if need in local_ids:
                        depended_on.add(need)
                config = dict(step.config)
                if recipe_id != change_id and step.step_type == "install_package_artifact":
                    # Infraestructura compartida (p. ej. AppImageLauncher) no se
                    # desinstala al retirar el cambio consumidor.
                    config["retain_on_rollback"] = True
                namespaced.append(replace(
                    step, id=mapping[step.id], source_id=step.source_id or step.id,
                    needs=list(dict.fromkeys(needs)), block=recipe_id, config=config,
                ))
            steps.extend(namespaced)
            terminals = [mapping[item] for item in mapping if item not in depended_on]
            previous_terminals = terminals or previous_terminals
            phases.update(compiled.phases)
        target = self._declarative_changes[change_id]
        return WorkflowDefinition(
            name=target.recipe.name, description=target.description, operation="apply",
            steps=steps, phases=phases,
            metadata={"recipe_id": change_id, "definition_source": "yaml", "composed_changes": list(order)},
        )

    # ---------------------------------------------------------- portable DAGs
    @staticmethod
    def _portable_change_id(package_id: str, graph_id: str) -> str:
        """Identidad estable del cambio sin colisionar con DAG incorporados.

        El usuario nunca necesita ver este prefijo. Internamente evita que un
        paquete pueda reemplazar por accidente a ``photogimp`` u otro cambio
        incorporado que use el mismo ``graph_id``.
        """
        candidate = f"pkg.{package_id}.{graph_id}"
        if len(candidate) <= 128:
            return candidate
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:20]
        return f"pkg.{package_id[:80]}.{digest}"[:128].rstrip(".-_")

    @staticmethod
    def _package_graph_count(package: InstalledPackage) -> int:
        return sum(1 for artifact in package.manifest.artifacts if artifact.kind == "graph")

    def _portable_change_sources(self) -> tuple[tuple[str, InstalledPackage, GraphDefinition], ...]:
        """DAG aportados por paquetes ``change`` registrados.

        Importar un paquete solo registra el artefacto. La ejecución sigue
        ocurriendo exclusivamente por ``ChangeService`` desde la pestaña
        Cambios, igual que PhotoGIMP.
        """
        sources: list[tuple[str, InstalledPackage, GraphDefinition]] = []
        latest: dict[str, InstalledPackage] = {}
        for package in self._portable_library.list_packages():
            if package.manifest.package_type is not PackageType.CHANGE:
                continue
            current = latest.get(package.manifest.package_id)
            if current is None or package.imported_at >= current.imported_at:
                latest[package.manifest.package_id] = package
        for package in latest.values():
            for artifact in package.manifest.artifacts:
                if artifact.kind != "graph":
                    continue
                raw = self._portable_library.read_artifact(package, artifact)
                graph = GraphDefinition.from_dict(json.loads(raw.decode("utf-8")))
                change_id = self._portable_change_id(package.manifest.package_id, graph.graph_id)
                sources.append((change_id, package, graph))
        return tuple(sources)

    def _portable_source(self, change_id: str) -> tuple[InstalledPackage, GraphDefinition] | None:
        for candidate, package, graph in self._portable_change_sources():
            if candidate == change_id:
                return package, graph
        return None

    def _is_portable_change(self, change_id: str) -> bool:
        return self._portable_source(change_id) is not None

    def _build_portable_plan(self, change_id: str) -> ChangePlan:
        """Envuelve un DAG portable en la experiencia existente de Cambios.

        No recompila, reordena ni sustituye el workflow del paquete. Solo
        construye las fases legibles que ``ChangeReviewScreen`` ya consume.
        """
        source = self._portable_source(change_id)
        if source is None:
            raise ValueError(
                "El paquete que define este cambio ya no está disponible."
            )
        package, graph = source
        workflow = graph.workflow
        phases = self._phases_for_workflow(workflow, automatic=True)
        name = package.manifest.name if self._package_graph_count(package) == 1 else graph.title
        description = graph.description or package.manifest.description
        summary = description or (
            f"Styler aplicará el DAG «{graph.title}» contenido en {package.identity}."
        )
        notice = (
            f"Origen: {package.identity}. El DAG se ejecutará con el mismo motor PipeCraft "
            "que usa Styler para los demás cambios."
        )
        undo_workflow = self.rollback_plan(change_id) if self.can_rollback(change_id) else None
        return ChangePlan(
            change_id=change_id,
            name=name,
            provider_id="stylerpkg",
            provider_label="DAG de paquete .stylerpkg",
            automation_level=AutomationLevel.AUTOMATIC,
            summary=summary,
            notice=notice,
            phases=phases,
            workflow=workflow,
            undo_workflow=undo_workflow,
            options={},
            reconciled_steps={},
            continuation_mode=False,
            operation="apply",
        )

    # ----------------------------------------------------------------- storage
    def _load_preferences(self) -> dict[str, Any]:
        return self._read_json(self._preferences_path)

    def _load_records(self) -> dict[str, Any]:
        return self._read_json(self._records_path)

    def _save_record(self, change_id: str, values: dict[str, Any]) -> None:
        records = self._load_records()
        previous = dict(records.get(change_id, {}))
        previous.update(values)
        previous["updated_at"] = time.time()
        records[change_id] = previous
        try:
            self._write_json(self._records_path, records)
        except OSError as exc:
            raise ChangeStateWriteError(self._records_path, exc) from exc

    def _state_write_failure_result(
        self,
        *,
        plan: ChangePlan,
        exc: ChangeStateWriteError,
        after_effects: bool,
        run_report: str = "",
        details: tuple[str, ...] = (),
    ) -> ChangeExecutionResult:
        errno_value = getattr(exc.original, "errno", None)
        if errno_value == errno.EROFS:
            cause = "El sistema de archivos que contiene el estado de Styler está montado en solo lectura."
        elif errno_value in {errno.EACCES, errno.EPERM}:
            cause = "Styler no tiene permiso de escritura sobre su almacenamiento de estado."
        else:
            cause = f"No se pudo escribir el almacenamiento de estado: {exc.original}."
        action_word = "retiro" if plan.operation == "remove" else "DAG"
        phase = (
            f"después de iniciar el {action_word}"
            if after_effects
            else "antes de modificar el equipo"
        )
        message = (
            f"{cause} El problema apareció {phase}. "
            + (
                "Styler detuvo el flujo para no seguir modificando el equipo sin poder registrar su estado."
                if after_effects
                else f"No se inició el {action_word}."
            )
        )
        diagnostic = self._write_emergency_state_diagnostic(
            plan=plan, exc=exc, after_effects=after_effects, run_report=run_report
        )
        state_line = f"Ruta afectada: {exc.path}"
        mount_line = _mount_status(exc.path.parent)
        extra = list(details)
        if after_effects:
            extra.insert(0, (
                "⚠ La reversión pudo haber deshecho efectos antes de que fallara el estado; "
                "los recibos existentes se conservan para poder reanudar el retiro."
                if plan.operation == "remove"
                else "⚠ El DAG pudo haber producido efectos antes de que fallara el estado; "
                     "los recibos existentes se conservan para poder retirarlos."
            ))
        extra.extend((state_line, mount_line))
        return ChangeExecutionResult(
            change_id=plan.change_id,
            name=plan.name,
            ok=False,
            status=ChangeStatus.NEEDS_ATTENTION if after_effects else ChangeStatus.FAILED,
            title=(
                (f"El retiro de {plan.name} empezó, pero Styler perdió acceso a su estado"
                 if plan.operation == "remove"
                 else f"{plan.name} empezó, pero Styler perdió acceso a su estado")
                if after_effects
                else (f"No se inició el retiro de {plan.name}" if plan.operation == "remove" else f"No se inició {plan.name}")
            ),
            message=message,
            provider_id=plan.provider_id,
            provider_label=plan.provider_label,
            automation_level=plan.automation_level,
            details=tuple(extra),
            options=dict(plan.options),
            diagnostic_path=diagnostic or run_report,
            operation=plan.operation,
        )

    def _write_emergency_state_diagnostic(
        self,
        *,
        plan: ChangePlan,
        exc: ChangeStateWriteError,
        after_effects: bool,
        run_report: str,
    ) -> str:
        """Guarda un diagnóstico fuera de la biblioteca si ésta deja de ser escribible.

        No sustituye el registro persistente ni se usa como fuente de verdad. Solo
        evita perder la explicación técnica cuando el propio almacén de estado
        es precisamente lo que falló.
        """
        try:
            root = Path(tempfile.gettempdir()) / f"styler-recovery-{os.getuid()}"
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"{plan.change_id.replace('/', '_')}-{int(time.time())}.json"
            payload = {
                "schema": "styler.state-write-diagnostic/1",
                "change_id": plan.change_id,
                "change_name": plan.name,
                "after_effects": after_effects,
                "state_path": str(exc.path),
                "errno": getattr(exc.original, "errno", None),
                "error": str(exc.original),
                "mount": _mount_status(exc.path.parent),
                "run_report": run_report,
                "created_at": time.time(),
            }
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            return str(path)
        except OSError:
            return ""

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, indent=2, ensure_ascii=False))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ helpers
    def _provider_option(self, provider_id: str) -> ProviderOption:
        option = next((item for item in self.provider_options("photogimp") if item.provider_id == provider_id), None)
        if option is None:
            raise ValueError(f"El proveedor '{provider_id}' no es compatible con este equipo.")
        return option

    @staticmethod
    def _provider_command_available(provider_id: str) -> bool:
        commands = PROVIDER_COMMANDS.get(provider_id, ())
        if not commands:
            return provider_id == "appimage"
        return any(shutil.which(command) for command in commands)

    @staticmethod
    def _status_label(status: str) -> str:
        return {
            ChangeStatus.PREPARED: "Preparado · falta integración manual",
            ChangeStatus.FAILED: "Falló",
            ChangeStatus.NEEDS_ATTENTION: "Necesita atención",
            ChangeStatus.PARTIALLY_REVERTED: "Revertido parcialmente",
            ChangeStatus.REVERTED: "Revertido",
            ChangeStatus.REVERTING: "Deshaciendo",
            ChangeStatus.INTEGRATING: "Integrando",
            ChangeStatus.INTEGRATED: "Integrado",
        }.get(status, "Estado desconocido")

    @staticmethod
    def _validate_change_id(change_id: str) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", change_id):
            raise ValueError(f"Identificador de cambio inválido: {change_id!r}.")

    def _require_change(self, change_id: str) -> None:
        self._validate_change_id(change_id)
        if change_id == "photogimp" or self._is_portable_change(change_id) or change_id in self._declarative_changes:
            return
        raise ValueError(f"El cambio '{change_id}' no está disponible en este Styler.")
