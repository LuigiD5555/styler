"""Ejecutores para los ``step_type`` que produce ``compiler.py``.

``install_package`` y ``enable_service`` ya existen en
``styler.execution.executors`` y se reutilizan tal cual. Este módulo agrega
los que son propios del catálogo de componentes: verificación declarativa,
respaldo antes de un overlay/configuración, y aplicación de un overlay
(PhotoGIMP-style) desde un asset ``catalog://``.

Todas las rutas pasan por ``styler.component_catalog.paths``, que rechaza
cualquier destino fuera del HOME del usuario y cualquier asset fuera del
directorio de assets del catálogo.
"""
from __future__ import annotations
from styler.execution.registry import default_registry

import json
import os
import shutil
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from styler.component_catalog.paths import PathResolutionError, expand_user_path
from styler.flatpak_facts import (
    inspect_flatpak_application,
    load_flatpak_facts,
    save_flatpak_facts,
)
from styler.receipts import (
    ReceiptJournal,
    ReceiptKind,
    ReceiptWriteError,
    emit_receipt,
    ensure_receipts_writable,
    prune_system_checkpoints,
)
from styler.execution.processes import run_step_command
from styler.execution.base import ExecutorRegistry, StepExecutor, emit_step_progress
from styler.planning.models import ExecutionContext, Status, StepDefinition, StepResult
from .executor_utils import _home_of, _run_probe, _target_path
from .gimp_runtime import (
    InitializeFlatpakAppExecutor, ResolveFlatpakAppFactsExecutor, _VERSION_DIR,
    _gimp_config_path_for_step, _verify_overlay_manifest,
)
from .photogimp_overlay import (
    PHOTOGIMP_RELEASE_PREFIX, CopyEffects, _apply_overlay, _backup_existing_file,
    _ensure_directory, _styler_user_agent,
)

BACKUP_ROOT = ".styler/component-backups"
CHECKPOINT_ROOT = ".styler/checkpoints"
SYSTEM_CHECKPOINT_LIMIT = 5




class VerifyExecutor(StepExecutor):
    """Corre las comprobaciones declaradas en ``[verification].checks``.

    - ``executable:<bin>``  → ¿está en el PATH?
    - ``directory:<recurso>`` → ¿existe la ruta real del componente?
    - ``marker:<nombre>``   → ¿existe el marcador dentro de esa ruta?
    - ``flatpak:<app-id>``   → ¿está instalada la aplicación Flatpak?
    - ``photogimp:overlay``  → ¿el manifiesto aplicado coincide con la carpeta real?
    """

    @property
    def step_type(self) -> str:
        return "verify"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        checks = list(step.config.get("checks") or [])
        if not checks:
            return StepResult.failed(
                step,
                "El componente no declara ninguna comprobación en [verification].",
                "NO_VERIFICATION_CHECKS",
                "Sin comprobaciones, Styler no puede afirmar que el componente quedó instalado.",
            )

        if ctx.dry_run:
            return StepResult(
                step.id, step.step_type, True, Status.DRY_RUN,
                f"Se comprobarían: {', '.join(checks)}.",
                data={"checks": checks},
            )

        try:
            target = _target_path(step, ctx)
        except PathResolutionError as exc:
            return StepResult.failed(step, str(exc), "UNSAFE_TARGET_PATH")

        results: dict[str, bool] = {}
        details: dict[str, object] = {}
        for index, check in enumerate(checks, 1):
            emit_step_progress(
                ctx, step, (index - 1) / max(1, len(checks)),
                f"Comprobando {check}…",
            )
            kind, _, value = check.partition(":")
            if kind == "executable":
                results[check] = shutil.which(value) is not None
            elif kind == "directory":
                if target is None:
                    return StepResult.failed(
                        step,
                        f"La comprobación '{check}' necesita una ruta real y el componente no declara ninguna.",
                        "NO_TARGET_FOR_CHECK",
                        "Declara la ruta en [resources.paths] o en el 'config_root' del proveedor.",
                    )
                results[check] = target.is_dir()
            elif kind == "marker":
                if target is None:
                    return StepResult.failed(
                        step,
                        f"La comprobación '{check}' necesita una ruta real y el componente no declara ninguna.",
                        "NO_TARGET_FOR_CHECK",
                    )
                results[check] = (target / f".{value}-marker").exists()
            elif kind == "flatpak":
                if not shutil.which("flatpak"):
                    results[check] = False
                else:
                    probe = run_step_command(
                        ctx, step, ["flatpak", "info", value], timeout=20,
                        label=f"Verificando el Flatpak {value}",
                    )
                    results[check] = probe.returncode == 0
            elif kind == "photogimp" and value == "overlay":
                if target is None:
                    return StepResult.failed(
                        step,
                        "La verificación de PhotoGIMP necesita la raíz de configuración de GIMP.",
                        "NO_TARGET_FOR_CHECK",
                    )
                marker = target / ".photogimp-marker"
                manifest_path = target / ".photogimp-manifest.json"
                try:
                    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                    target_version = str(payload.get("target_config_version") or "")
                    files = payload.get("files") or {}
                    if not marker.is_file() or not _VERSION_DIR.fullmatch(target_version) or not isinstance(files, dict):
                        raise ValueError("marcador o manifiesto incompleto")
                    destination = target / target_version
                    mismatches = _verify_overlay_manifest(
                        {str(key): str(value) for key, value in files.items()},
                        destination,
                    )
                    results[check] = destination.is_dir() and not mismatches
                    details[check] = {
                        "manifest": str(manifest_path),
                        "target": str(destination),
                        "files": len(files),
                        "mismatches": mismatches[:20],
                        "source_config_version": str(payload.get("source_config_version") or ""),
                        "target_config_version": target_version,
                        "adaptation_mode": str(payload.get("adaptation_mode") or ""),
                    }
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    results[check] = False
                    details[check] = {"error": str(exc), "manifest": str(manifest_path)}
            else:
                return StepResult.failed(
                    step, f"Tipo de comprobación desconocido: '{kind}'.", "UNKNOWN_CHECK_TYPE"
                )

        emit_step_progress(ctx, step, 1.0, "Comprobaciones terminadas.")
        failed_checks = [name for name, ok in results.items() if not ok]
        if failed_checks:
            return StepResult.failed(
                step, f"Comprobación fallida: {', '.join(failed_checks)}.", "VERIFICATION_FAILED"
            )
        return StepResult(
            step.id, step.step_type, True, Status.OK,
            f"Comprobaciones satisfechas: {', '.join(results)}.",
            data={"checks": results, "details": details},
        )




class BackupConfigExecutor(StepExecutor):
    """Respalda configuración de usuario existente antes de un overlay/config."""

    @property
    def step_type(self) -> str:
        return "backup_config"

    def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        if not bool(ctx.values.get("continuation_mode")):
            return None
        change_id = str(ctx.values.get("change_id") or "")
        if not change_id:
            return None
        try:
            journal = ReceiptJournal(ctx.values.get("receipts_root") or ctx.root, change_id)
            receipt = journal.latest_pending_for_step(step.id, kind=ReceiptKind.BACKUP_CREATED)
        except (OSError, ValueError):
            return None
        if receipt is None:
            return None
        existed = bool(receipt.data.get("existed"))
        backup = str(receipt.data.get("backup") or "")
        if existed and (not backup or not Path(backup).exists()):
            return None
        return StepResult(
            step.id,
            step.step_type,
            True,
            Status.RECONCILED,
            "El respaldo de la ejecución anterior sigue disponible; no se duplicará.",
            data={
                "reconciled": True,
                "evidence": "receipt",
                "receipt_id": receipt.receipt_id,
                "source": str(receipt.data.get("source") or ""),
                "backup": backup,
                "existed": existed,
            },
        )

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        raw = str(step.config.get("backup_source") or "")
        if not raw:
            return StepResult.failed(
                step,
                "El componente no declara una ruta real de configuración de usuario a respaldar.",
                "NO_BACKUP_SOURCE",
                "Declara la ruta en [resources.paths], o el 'config_root' del proveedor del que depende.",
            )
        if ctx.dry_run:
            try:
                preview_path = expand_user_path(raw, home=_home_of(ctx))
            except PathResolutionError as exc:
                return StepResult.failed(step, str(exc), "UNSAFE_BACKUP_SOURCE")
            return StepResult(
                step.id, step.step_type, True, Status.DRY_RUN,
                f"Se resolvería la versión de GIMP y se respaldaría su carpeta bajo {preview_path}.",
                data={"source": str(preview_path), "runtime_facts": bool(step.config.get("runtime_facts_application_id"))},
            )

        facts: dict[str, object] = {}
        runtime_app_id = str(step.config.get("runtime_facts_application_id") or "")
        try:
            if runtime_app_id:
                _, facts, source_path = _gimp_config_path_for_step(step, ctx, raw)
                if not str(facts.get("config_schema") or ""):
                    return StepResult.failed(
                        step,
                        "No se pudo resolver la versión de GIMP antes del respaldo.",
                        "GIMP_CONFIG_SCHEMA_UNRESOLVED",
                    )
                if bool(step.config.get("require_initialized_cycle", False)):
                    initialized_path = str(facts.get("initialized_config_path") or "")
                    path_independent = bool(step.config.get("initialization_path_independent", False))
                    invalid_path = (not path_independent and initialized_path != str(source_path))
                    if not bool(facts.get("initialization_completed")) or invalid_path:
                        return StepResult.failed(
                            step,
                            "GIMP no tiene evidencia de un ciclo completo de apertura y cierre para esta versión.",
                            "GIMP_INITIALIZATION_EVIDENCE_MISSING",
                            "Ejecuta primero la fase de inicialización controlada; Styler no respaldará una carpeta parcial.",
                        )
                running, _, state_detail = InitializeFlatpakAppExecutor._flatpak_state(runtime_app_id)
                if running:
                    return StepResult.failed(
                        step,
                        f"{runtime_app_id} sigue abierto; no se puede crear un respaldo consistente.",
                        "APP_MUST_BE_CLOSED_FOR_BACKUP",
                        f"Cierra GIMP y vuelve a continuar. Estado detectado: {state_detail}",
                    )
            else:
                source_path = expand_user_path(raw, home=_home_of(ctx))
        except PathResolutionError as exc:
            return StepResult.failed(step, str(exc), "UNSAFE_BACKUP_SOURCE")

        try:
            ensure_receipts_writable(ctx)
        except ReceiptWriteError as exc:
            return StepResult.failed(
                step, str(exc), "RECEIPT_JOURNAL_UNAVAILABLE",
                "Styler no modificó la configuración porque no podía garantizar el registro de reversión.",
            )

        emit_step_progress(ctx, step, 0.10, f"Revisando {source_path}…")
        if not source_path.exists():
            emit_step_progress(ctx, step, 1.0, "No hay configuración anterior que respaldar.")
            # Recibo igualmente: "no existía" es justo lo que el rollback
            # necesita saber para quitar lo que el cambio creó desde cero.
            emit_receipt(
                ctx, step, ReceiptKind.BACKUP_CREATED,
                {"source": str(source_path), "backup": "", "existed": False},
            )
            return StepResult(
                step.id, step.step_type, True, Status.OK,
                f"No existe {source_path} todavía; no hay nada que respaldar.",
                data={"source": str(source_path), "existed": False},
            )

        destination = ctx.root / BACKUP_ROOT / step.id.replace(".", "-") / str(int(time.time()))
        emit_step_progress(ctx, step, 0.30, "Copiando la configuración al respaldo…")
        try:
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / source_path.name
            if source_path.is_dir():
                shutil.copytree(source_path, target)
            else:
                shutil.copy2(source_path, target)
        except OSError as exc:
            return StepResult.failed(step, f"No se pudo respaldar {source_path}: {exc}", "BACKUP_FAILED")

        emit_step_progress(ctx, step, 1.0, f"Respaldo creado en {target}.")
        emit_receipt(
            ctx, step, ReceiptKind.BACKUP_CREATED,
            {"source": str(source_path), "backup": str(target), "existed": True},
        )
        return StepResult(
            step.id, step.step_type, True, Status.OK,
            f"Respaldo creado en {target}.",
            data={"source": str(source_path), "backup": str(target), "existed": True},
        )


class CreateChangeCheckpointExecutor(StepExecutor):
    """Crea un punto de retorno antes del primer efecto de una integración."""

    @property
    def step_type(self) -> str:
        return "create_change_checkpoint"

    def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        if not bool(ctx.values.get("continuation_mode")):
            return None
        change_id = str(ctx.values.get("change_id") or "")
        if not change_id:
            return None
        try:
            journal = ReceiptJournal(ctx.values.get("receipts_root") or ctx.root, change_id)
            receipt = journal.latest_pending_for_step(step.id, kind=ReceiptKind.CHECKPOINT_CREATED)
        except (OSError, ValueError):
            return None
        if receipt is None:
            return None
        missing_backup = False
        for item in receipt.data.get("paths") or []:
            if not isinstance(item, dict) or not item.get("existed"):
                continue
            backup = str(item.get("backup") or "")
            if not backup or not Path(backup).exists():
                missing_backup = True
                break
        if missing_backup:
            return None
        return StepResult(
            step.id,
            step.step_type,
            True,
            Status.RECONCILED,
            "El checkpoint inicial de la ejecución anterior sigue disponible.",
            data={
                "reconciled": True,
                "evidence": "receipt",
                "receipt_id": receipt.receipt_id,
                "checkpoint_id": str(receipt.data.get("checkpoint_id") or ""),
            },
        )

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        if ctx.dry_run:
            return StepResult(
                step.id,
                step.step_type,
                True,
                Status.DRY_RUN,
                "Se crearía un checkpoint reversible antes de modificar el equipo.",
                data={
                    "scope": str(step.config.get("scope") or "change"),
                    "paths": list(step.config.get("paths") or []),
                    "packages": list(step.config.get("packages") or []),
                },
            )

        try:
            ensure_receipts_writable(ctx)
        except ReceiptWriteError as exc:
            return StepResult.failed(
                step,
                str(exc),
                "RECEIPT_JOURNAL_UNAVAILABLE",
                "Styler no modificó nada porque no podía crear un checkpoint reversible.",
            )

        scope = str(step.config.get("scope") or "change")
        change_id = str(ctx.values.get("change_id") or "change")
        raw_checkpoint_id = str(step.config.get("checkpoint_id") or change_id)
        checkpoint_id = f"{raw_checkpoint_id}-{ctx.run_id or int(time.time())}"
        checkpoint_dir = ctx.root / CHECKPOINT_ROOT / checkpoint_id
        checkpoint_paths: list[dict[str, object]] = []
        packages: list[dict[str, object]] = []
        home = _home_of(ctx)

        for raw in step.config.get("paths") or []:
            try:
                source = expand_user_path(str(raw), home=home)
            except PathResolutionError as exc:
                return StepResult.failed(step, str(exc), "UNSAFE_CHECKPOINT_PATH")
            entry: dict[str, object] = {"path": str(source), "existed": source.exists() or source.is_symlink(), "backup": ""}
            if entry["existed"]:
                destination = checkpoint_dir / "paths" / str(len(checkpoint_paths)) / source.name
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if source.is_dir() and not source.is_symlink():
                        shutil.copytree(source, destination, symlinks=True)
                    else:
                        shutil.copy2(source, destination)
                except OSError as exc:
                    return StepResult.failed(
                        step,
                        f"No se pudo crear checkpoint de {source}: {exc}",
                        "CHECKPOINT_BACKUP_FAILED",
                    )
                entry["backup"] = str(destination)
            checkpoint_paths.append(entry)

        from styler.execution.executors import PackageInstallExecutor

        for item in step.config.get("packages") or []:
            if not isinstance(item, dict):
                continue
            manager = str(item.get("manager") or "")
            package = str(item.get("name") or item.get("package") or "")
            if not manager or not package:
                continue
            was_present = PackageInstallExecutor._is_installed(manager, package)
            package_entry: dict[str, object] = {
                "manager": manager,
                "package": package,
                "was_present": was_present,
            }
            if manager == "flatpak" and was_present:
                observed = inspect_flatpak_application(package)
                package_entry["facts"] = observed.to_dict()
                # El checkpoint conserva la observación en su propio recibo.
                # No debe sobrescribir hechos enriquecidos por la inicialización
                # (ruta exacta y ciclo de apertura/cierre), porque eso haría que
                # la siguiente fase olvidara una evidencia válida. Si aún no
                # existe archivo de hechos, se crea uno básico; el nodo
                # resolve-facts lo actualizará después.
                if observed.installed and load_flatpak_facts(ctx.root, package) is None:
                    save_flatpak_facts(ctx.root, observed)
            packages.append(package_entry)

        receipt_data = {
            "checkpoint_id": checkpoint_id,
            "scope": scope,
            "paths": checkpoint_paths,
            "packages": packages,
            "change_id": change_id,
            "run_id": ctx.run_id,
        }
        emit_receipt(ctx, step, ReceiptKind.CHECKPOINT_CREATED, receipt_data)
        prune_system_checkpoints(ctx.root, keep=SYSTEM_CHECKPOINT_LIMIT)
        emit_step_progress(ctx, step, 1.0, f"Checkpoint creado: {checkpoint_id}.")
        return StepResult(
            step.id,
            step.step_type,
            True,
            Status.OK,
            f"Checkpoint inicial creado: {checkpoint_id}.",
            data=receipt_data,
        )




class OverlayInstallExecutor(StepExecutor):
    """Aplica un overlay declarado con proveedor ``archive`` (PhotoGIMP-style)."""

    @property
    def step_type(self) -> str:
        return "install_overlay"

    def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        # Un marcador solo se reutiliza al CONTINUAR una ejecución fallida. Si
        # la persona elige integrar de nuevo un cambio ya terminado, se trata
        # como reparación y el overlay sí puede volver a aplicarse.
        if not bool(ctx.values.get("continuation_mode")):
            return None
        raw_target = str(step.config.get("target") or "")
        if not raw_target:
            return None
        try:
            target = expand_user_path(raw_target, home=_home_of(ctx))
        except PathResolutionError:
            return None
        markers = [target / ".photogimp-marker"]
        if target.is_dir():
            markers.extend(
                item / ".photogimp-marker"
                for item in target.iterdir()
                if item.is_dir() and _VERSION_DIR.fullmatch(item.name)
            )
        marker = next((item for item in markers if item.is_file()), None)
        manifest_path = target / ".photogimp-manifest.json"
        if marker is None or not manifest_path.is_file():
            return None
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            target_version = str(payload.get("target_config_version") or "")
            files = payload.get("files") or {}
            if not _VERSION_DIR.fullmatch(target_version) or not isinstance(files, dict):
                return None
            version_target = target / target_version
            mismatches = _verify_overlay_manifest(
                {str(key): str(value) for key, value in files.items()},
                version_target,
            )
            if not version_target.is_dir() or mismatches:
                return None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return StepResult(
            step.id,
            step.step_type,
            True,
            Status.RECONCILED,
            "La copia anterior de PhotoGIMP coincide con su manifiesto; se continuará con la verificación.",
            data={
                "reconciled": True,
                "evidence": "marker+manifest+hashes",
                "marker": str(marker),
                "manifest": str(manifest_path),
                "target": str(version_target),
                "files": len(files),
            },
        )

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        return _apply_overlay(step, ctx)




def _visual_setting_command(config: dict[str, object], *, write: bool) -> list[str] | None:
    backend = str(config.get("backend") or "")
    schema = str(config.get("schema") or "")
    group = str(config.get("group") or "")
    key = str(config.get("key") or "")
    value = str(config.get("value") or "")
    if backend == "gsettings":
        if not shutil.which("gsettings") or not schema or not key:
            return None
        return ["gsettings", "set" if write else "get", schema, key, *([value] if write else [])]
    if backend == "kconfig":
        tool = (
            shutil.which("kwriteconfig6") or shutil.which("kwriteconfig5")
            if write
            else shutil.which("kreadconfig6") or shutil.which("kreadconfig5")
        )
        if not tool or not schema or not group or not key:
            return None
        argv = [tool, "--file", schema, "--group", group, "--key", key]
        if write:
            argv.append(value)
        return argv
    return None


def _read_visual_setting(config: dict[str, object]) -> tuple[bool, str, str]:
    argv = _visual_setting_command(config, write=False)
    if argv is None:
        return False, "", "No está disponible la herramienta necesaria para leer el ajuste."
    result = _run_probe(argv, timeout=10)
    if result.returncode != 0:
        return False, "", result.stderr.strip() or result.stdout.strip() or "La lectura del ajuste falló."
    return True, result.stdout.strip(), ""


class ApplyVisualSettingExecutor(StepExecutor):
    """Aplica GSettings o KConfig como acción declarativa y reversible."""

    @property
    def step_type(self) -> str:
        return "apply_visual_setting"

    def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        config = dict(step.config)
        ok, current, _error = _read_visual_setting(config)
        desired = str(config.get("value") or "")
        if not ok or current != desired:
            return None
        return StepResult(
            step.id, step.step_type, True, Status.RECONCILED,
            f"El ajuste {config.get('key', '')} ya tiene el valor solicitado.",
            data={"current": current, "reconciled": True},
        )

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        config = dict(step.config)
        backend = str(config.get("backend") or "")
        schema = str(config.get("schema") or "")
        group = str(config.get("group") or "")
        key = str(config.get("key") or "")
        desired = str(config.get("value") or "")
        if backend not in {"gsettings", "kconfig"} or not schema or not key or not desired:
            return StepResult.failed(step, "El ajuste visual está incompleto.", "VISUAL_SETTING_CONFIG_ERROR")
        if backend == "kconfig" and not group:
            return StepResult.failed(step, "El ajuste KConfig no declara un grupo.", "VISUAL_SETTING_CONFIG_ERROR")

        ok, previous, error = _read_visual_setting(config)
        if not ok:
            return StepResult.failed(step, f"No se pudo leer el ajuste antes de cambiarlo: {error}", "VISUAL_SETTING_READ_FAILED")
        if previous == desired:
            return StepResult(
                step.id, step.step_type, True, Status.RECONCILED,
                f"El ajuste {key} ya coincide.", data={"current": previous, "reconciled": True},
            )
        if ctx.dry_run:
            return StepResult(
                step.id, step.step_type, True, Status.DRY_RUN,
                f"Se cambiaría {backend}:{schema}:{key} de {previous!r} a {desired!r}.",
                data={"previous": previous, "desired": desired},
            )
        try:
            ensure_receipts_writable(ctx)
        except ReceiptWriteError as exc:
            return StepResult.failed(step, str(exc), "RECEIPT_JOURNAL_UNAVAILABLE")

        argv = _visual_setting_command(config, write=True)
        if argv is None:
            return StepResult.failed(step, "No está disponible la herramienta para escribir el ajuste.", "VISUAL_SETTING_TOOL_MISSING")
        command = run_step_command(
            ctx, step, argv, timeout=step.timeout,
            label=f"Aplicando ajuste visual {key}",
        )
        if command.returncode != 0:
            return StepResult.failed(
                step, f"No se pudo aplicar el ajuste {key}: {command.stderr or command.stdout}",
                "VISUAL_SETTING_APPLY_FAILED",
            )
        ok, observed, error = _read_visual_setting(config)
        if not ok or observed != desired:
            return StepResult.failed(
                step,
                f"El ajuste se ejecutó, pero la verificación no coincide: {error or observed!r}",
                "VISUAL_SETTING_VERIFY_FAILED",
            )
        emit_receipt(
            ctx, step, ReceiptKind.SETTING_CHANGED,
            {
                "backend": backend,
                "schema": schema,
                "group": group,
                "key": key,
                "value": desired,
                "previous_value": previous,
                "previous_exists": bool(previous),
            },
        )
        return StepResult(
            step.id, step.step_type, True, Status.OK,
            f"Ajuste aplicado: {backend}:{schema}:{key}.",
            data={"previous": previous, "value": desired},
        )

class VerifyGeneratedChangeExecutor(StepExecutor):
    """Comprueba paquetes y recursos declarados por una receta generada."""

    @property
    def step_type(self) -> str:
        return "verify_generated_change"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        checks = step.config.get("checks") or []
        if ctx.dry_run:
            return StepResult(
                step.id, step.step_type, True, Status.DRY_RUN,
                f"Se verificarían {len(checks)} efectos del cambio generado.",
                data={"checks": checks},
            )
        failures: list[str] = []
        verified: list[str] = []
        from styler.execution.executors import PackageInstallExecutor
        from styler.provenance.artifacts import checksum_path
        for raw in checks:
            if not isinstance(raw, dict):
                failures.append("comprobación inválida")
                continue
            kind = str(raw.get("kind") or "")
            if kind == "package":
                manager, name = str(raw.get("manager") or ""), str(raw.get("name") or "")
                if manager and name and PackageInstallExecutor._is_installed(manager, name):
                    verified.append(f"{manager}:{name}")
                else:
                    failures.append(f"paquete ausente: {manager}:{name}")
            elif kind == "artifact":
                raw_path = str(raw.get("path") or "")
                expected = str(raw.get("checksum") or "")
                try:
                    path = expand_user_path(raw_path, home=_home_of(ctx))
                    actual, _size, _count, _directory = checksum_path(path)
                except (OSError, ValueError, PathResolutionError) as exc:
                    failures.append(f"recurso no verificable {raw_path}: {exc}")
                    continue
                if expected and actual != expected:
                    failures.append(f"checksum diferente: {raw_path}")
                else:
                    verified.append(raw_path)
            elif kind == "setting":
                config = {key: value for key, value in raw.items() if key != "kind"}
                ok, observed, error = _read_visual_setting(config)
                expected = str(config.get("value") or "")
                label = f"{config.get('backend', '')}:{config.get('schema', '')}:{config.get('key', '')}"
                if not ok:
                    failures.append(f"ajuste no verificable {label}: {error}")
                elif observed != expected:
                    failures.append(f"valor diferente en {label}: {observed!r}")
                else:
                    verified.append(label)
            else:
                failures.append(f"tipo de comprobación desconocido: {kind}")
        if failures:
            return StepResult.failed(
                step, "La verificación del cambio generado falló: " + "; ".join(failures),
                "GENERATED_CHANGE_VERIFY_FAILED",
            )
        return StepResult(
            step.id, step.step_type, True, Status.OK,
            f"Cambio verificado: {len(verified)} efectos coinciden.", data={"verified": verified},
        )


class ApplyConfigExecutor(StepExecutor):
    """Aplica una 'configuration' de usuario (sección 12: config KDE).

    Una configuración sin fuente declarada no escribe nada: los archivos de
    la personalización del usuario los aplica el pipeline de personalización
    ya existente (``styler.restore``), que trabaja desde el almacén de
    objetos. Este paso existe para que la configuración ocupe su lugar
    correcto en el DAG (después de que KDE esté verificado), no para
    duplicar ese trabajo.
    """

    @property
    def step_type(self) -> str:
        return "apply_config"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        if step.config.get("source"):
            return _apply_overlay(step, ctx)

        raw_target = str(step.config.get("target", ""))
        if ctx.dry_run:
            return StepResult(
                step.id, step.step_type, True, Status.DRY_RUN,
                f"El destino {raw_target or '(sin declarar)'} quedaría listo para la personalización.",
                data={"target": raw_target},
            )
        if not raw_target:
            return StepResult.failed(
                step, "El componente no declara un destino real.", "NO_CONFIG_TARGET"
            )
        try:
            target = expand_user_path(raw_target, home=_home_of(ctx))
        except PathResolutionError as exc:
            return StepResult.failed(step, str(exc), "UNSAFE_TARGET_PATH")

        try:
            ensure_receipts_writable(ctx)
        except ReceiptWriteError as exc:
            return StepResult.failed(step, str(exc), "RECEIPT_JOURNAL_UNAVAILABLE")
        already_existed = target.exists()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return StepResult.failed(step, f"No se pudo preparar {target}: {exc}", "CONFIG_TARGET_FAILED")
        if not already_existed:
            emit_receipt(
                ctx, step, ReceiptKind.DIRECTORY_CREATED,
                {"created_directories": [str(target)]},
            )
        return StepResult(
            step.id, step.step_type, True, Status.OK,
            f"Destino de configuración listo: {target}.",
            data={"target": str(target)},
        )


class ManualHandoffExecutor(StepExecutor):
    """Descarga un cambio y deja un traspaso manual explícito en Descargas."""

    @property
    def step_type(self) -> str:
        return "prepare_manual_handoff"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        source = str(step.config.get("source") or "")
        change_name = str(step.config.get("change_name") or "Cambio")
        provider_label = str(step.config.get("provider_label") or "la fuente elegida")
        if not source.startswith(PHOTOGIMP_RELEASE_PREFIX):
            return StepResult.failed(step, "Fuente remota no autorizada.", "UNTRUSTED_HANDOFF_SOURCE")
        if ctx.dry_run:
            return StepResult(
                step.id, step.step_type, True, Status.DRY_RUN,
                f"Se descargaría {change_name} en la carpeta de Descargas.",
            )
        home = _home_of(ctx) or Path.home()
        downloads = _xdg_download_dir(home)
        try:
            ensure_receipts_writable(ctx)
        except ReceiptWriteError as exc:
            return StepResult.failed(step, str(exc), "RECEIPT_JOURNAL_UNAVAILABLE")
        effects = CopyEffects()
        _ensure_directory(downloads, effects)
        backup_root = ctx.root / ".styler" / "write-backups" / (ctx.run_id or "pending") / step.id
        archive = downloads / "PhotoGIMP-linux.zip"
        partial = archive.with_suffix(archive.suffix + ".part")
        request = Request(source, headers={"User-Agent": _styler_user_agent()})
        try:
            emit_step_progress(ctx, step, 0.03, "Localizando la carpeta de Descargas…")
            with urlopen(request, timeout=45) as response, partial.open("wb") as output:
                total = int(getattr(response, "headers", {}).get("Content-Length") or 0)
                current = 0
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    current += len(chunk)
                    if total:
                        emit_step_progress(
                            ctx, step, 0.05 + (current / total) * 0.75,
                            f"Descargando PhotoGIMP · {current / 1048576:.1f} de {total / 1048576:.1f} MB",
                        )
                    else:
                        emit_step_progress(ctx, step, None, f"Descargando PhotoGIMP · {current / 1048576:.1f} MB")
            if not zipfile.is_zipfile(partial):
                partial.unlink(missing_ok=True)
                return StepResult.failed(step, "La descarga no es un ZIP válido.", "INVALID_HANDOFF_ARCHIVE")
            if archive.exists() or archive.is_symlink():
                backup = _backup_existing_file(archive, backup_root / "archive")
                effects.overwritten.append({"path": str(archive), "backup": str(backup)})
            else:
                effects.created_paths.append(str(archive))
            os.replace(partial, archive)
            emit_step_progress(ctx, step, 0.88, "Creando instrucciones para la integración manual…")
            instructions = downloads / "PhotoGIMP-INSTRUCCIONES.txt"
            if instructions.exists() or instructions.is_symlink():
                backup = _backup_existing_file(instructions, backup_root / "instructions")
                effects.overwritten.append({"path": str(instructions), "backup": str(backup)})
            else:
                effects.created_paths.append(str(instructions))
            instructions.write_text(
                f"""PhotoGIMP preparado por Styler

GIMP fue instalado mediante: {provider_label}
Archivo descargado: {archive}

PhotoGIMP recomienda la versión de GIMP distribuida mediante Flathub.
Styler todavía no tiene un pipeline automático validado para {provider_label}.
Por seguridad no copió archivos dentro de .config ni .local.

Siguiente paso:
1. Abre GIMP una vez y ciérralo.
2. Consulta las instrucciones oficiales incluidas con PhotoGIMP.
3. Integra manualmente el contenido del ZIP en las rutas que use esta instalación de GIMP.
4. Regresa a Styler cuando exista un modelo validado para automatizar esta variante.
""",
                encoding="utf-8",
            )
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            partial.unlink(missing_ok=True)
            return StepResult.failed(
                step, f"No se pudo preparar PhotoGIMP: {exc}", "MANUAL_HANDOFF_FAILED",
                "Comprueba la conexión y el espacio disponible en Descargas.",
            )
        emit_receipt(
            ctx, step, ReceiptKind.PATHS_WRITTEN, effects.receipt_data(target=downloads)
        )
        emit_step_progress(ctx, step, 1.0, f"PhotoGIMP quedó preparado en {archive}.")
        return StepResult(
            step.id, step.step_type, True, Status.OK_WITH_WARNINGS,
            f"PhotoGIMP se descargó en {archive}; la integración final es manual.",
            data={
                "handoff_path": str(archive),
                "instructions_path": str(instructions),
                "provider_label": provider_label,
            },
        )


def _xdg_download_dir(home: Path) -> Path:
    if shutil.which("xdg-user-dir"):
        result = _run_probe(["xdg-user-dir", "DOWNLOAD"], timeout=5)
        candidate = Path(result.stdout.strip()).expanduser() if result.stdout.strip() else None
        if result.returncode == 0 and candidate is not None and candidate != home:
            return candidate
    for name in ("Downloads", "Descargas"):
        candidate = home / name
        if candidate.is_dir():
            return candidate
    return home / "Downloads"


def extended_registry() -> ExecutorRegistry:
    """``default_registry()`` más los ejecutores del catálogo."""
    registry = default_registry()
    registry.register(VerifyExecutor())
    registry.register(ResolveFlatpakAppFactsExecutor())
    registry.register(InitializeFlatpakAppExecutor())
    registry.register(CreateChangeCheckpointExecutor())
    registry.register(BackupConfigExecutor())
    registry.register(OverlayInstallExecutor())
    registry.register(ApplyVisualSettingExecutor())
    registry.register(VerifyGeneratedChangeExecutor())
    from styler.appimage_actions import (
        AppImageIntegrateExecutor, AppImageVerifyExecutor, ExecutableVerifyExecutor,
        PackageInstallArtifactExecutor, ReleaseFetchExecutor,
    )
    registry.register(ReleaseFetchExecutor())
    registry.register(PackageInstallArtifactExecutor())
    registry.register(ExecutableVerifyExecutor())
    registry.register(AppImageIntegrateExecutor())
    registry.register(AppImageVerifyExecutor())
    registry.register(ApplyConfigExecutor())
    registry.register(ManualHandoffExecutor())
    return registry
