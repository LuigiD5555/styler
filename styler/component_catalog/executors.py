"""Ejecutores para los ``step_type`` que produce ``compiler.py``.

``install_package`` y ``enable_service`` ya existen en
``styler.runtime.executors`` y se reutilizan tal cual. Este módulo agrega
los que son propios del catálogo de componentes: verificación declarativa,
respaldo antes de un overlay/configuración, y aplicación de un overlay
(PhotoGIMP-style) desde un asset ``catalog://``.

Todas las rutas pasan por ``styler.component_catalog.paths``, que rechaza
cualquier destino fuera del HOME del usuario y cualquier asset fuera del
directorio de assets del catálogo.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from styler.automation.actions import ActionContext, ActionResult, FunctionAction
from styler.automation.conditions import (
    AnyCondition,
    CallableCondition,
    ConditionAborted,
    DirectoryQuiescentCondition,
    LatchedCondition,
    WaitResult,
    wait_until,
)
from styler.automation.diagnostics import capture_wait_failure
from styler.automation.controller import ApplicationController
from styler.automation.events import Event, EventType
from styler.automation.profiles import ApplicationProfile
from styler.component_catalog.paths import PathResolutionError, expand_user_path, resolve_catalog_uri
from styler.flatpak_facts import (
    FlatpakApplicationFacts,
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
from styler.runtime.commands import CommandResult, PipeCraftRunner, run_step_command, start_step_process
from styler.runtime.executors import ExecutorRegistry, StepExecutor, emit_step_progress
from styler.runtime.models import ExecutionContext, Status, StepDefinition, StepResult

BACKUP_ROOT = ".styler/component-backups"
CHECKPOINT_ROOT = ".styler/checkpoints"
SYSTEM_CHECKPOINT_LIMIT = 5


def _run_probe(
    argv: list[str],
    *,
    timeout: float = 10.0,
    env: dict[str, str] | None = None,
):
    """Sonda sin efectos por la frontera única de procesos de PipeCraft."""
    return PipeCraftRunner(timeout=timeout).run(argv, timeout=timeout, env=env)


def _home_of(ctx: ExecutionContext) -> Path | None:
    """HOME efectivo. Inyectable por prueba, igual que target/is_root en el resto
    del proyecto: sin esto no se puede probar la escritura real sin tocar el HOME
    de verdad de quien corre las pruebas."""
    injected = ctx.values.get("home")
    return Path(injected) if injected else None


def _target_path(step: StepDefinition, ctx: ExecutionContext) -> Path | None:
    raw = str(step.config.get("target") or step.config.get("backup_source") or "")
    if not raw:
        return None
    return expand_user_path(raw, home=_home_of(ctx))


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


_VERSION_DIR = re.compile(r"^\d+(?:\.\d+)+$")


def _version_key(name: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in name.split("."))
    except ValueError:
        return ()


def _normalized_version_key(name: str, width: int = 4) -> tuple[int, ...]:
    """Clave comparable para esquemas presentes y futuros (3.2, 4.0, 10.1)."""
    values = _version_key(name)
    if not values:
        return ()
    return (*values, *((0,) * max(0, width - len(values))))[:width]


def _select_photogimp_source_version(
    source_root: Path,
    target_schema: str,
) -> tuple[Path, str]:
    """Selecciona la plantilla de PhotoGIMP sin renombrar su carpeta.

    Prioridades:
    1. configuración exacta para la versión instalada;
    2. plantilla más reciente de la misma familia mayor;
    3. plantilla anterior más reciente para migración hacia una versión futura.

    Nunca se aplica una plantilla *más nueva* sobre un GIMP más antiguo. Así,
    una plantilla 3.0 puede adaptarse a GIMP 3.2 o 4.0, pero una plantilla 4.0
    no se fuerza sobre GIMP 3.2.
    """
    if not source_root.is_dir():
        raise ValueError("PhotoGIMP no contiene .config/GIMP con plantillas versionadas.")
    if not _VERSION_DIR.fullmatch(target_schema):
        raise ValueError(f"Esquema de configuración de GIMP inválido: {target_schema!r}.")

    candidates = sorted(
        (item for item in source_root.iterdir() if item.is_dir() and _VERSION_DIR.fullmatch(item.name)),
        key=lambda item: _normalized_version_key(item.name),
    )
    if not candidates:
        raise ValueError("PhotoGIMP no contiene ninguna plantilla de configuración versionada.")

    exact = source_root / target_schema
    if exact.is_dir():
        return exact, "exact"

    target_key = _normalized_version_key(target_schema)
    target_major = _version_key(target_schema)[0]
    same_major = [
        item for item in candidates
        if _version_key(item.name)[0] == target_major
        and _normalized_version_key(item.name) <= target_key
    ]
    if same_major:
        return max(same_major, key=lambda item: _normalized_version_key(item.name)), "same-major-template"

    older = [item for item in candidates if _normalized_version_key(item.name) <= target_key]
    if older:
        return max(older, key=lambda item: _normalized_version_key(item.name)), "forward-template"

    available = ", ".join(item.name for item in candidates)
    raise ValueError(
        f"PhotoGIMP solo incluye plantillas más nuevas que GIMP {target_schema}; "
        f"versiones disponibles: {available}."
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_manifest(root: Path) -> dict[str, str]:
    """Manifest exacto de archivos regulares de un árbol de overlay."""
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"No se admiten enlaces simbólicos en PhotoGIMP: {path}")
        if path.is_file():
            manifest[path.relative_to(root).as_posix()] = _sha256_file(path)
    return manifest


def _verify_overlay_manifest(source_manifest: dict[str, str], destination: Path) -> list[str]:
    """Comprueba que todos los archivos fuente llegaron al destino exacto."""
    mismatches: list[str] = []
    for relative, expected_hash in source_manifest.items():
        target = destination.joinpath(*PurePosixPath(relative).parts)
        if not target.is_file():
            mismatches.append(f"ausente:{relative}")
            continue
        try:
            current_hash = _sha256_file(target)
        except OSError:
            mismatches.append(f"ilegible:{relative}")
            continue
        if current_hash != expected_hash:
            mismatches.append(f"distinto:{relative}")
    return mismatches


def _detect_gimp_version_dir(
    config_root: Path,
    expected_schema: str = "",
) -> Path | None:
    """Localiza la carpeta de configuración real de la versión instalada.

    ``expected_schema`` proviene de Flatpak (3.0.4 -> 3.0). Se prioriza esa
    carpeta exacta; solo se usa otra carpeta numérica como recuperación para
    instalaciones antiguas cuyo gestor no reporta versión.
    """
    if config_root.is_dir() and expected_schema:
        exact = config_root / expected_schema
        if exact.is_dir():
            return exact
    if not config_root.is_dir():
        return None
    candidates = [
        item for item in config_root.iterdir()
        if item.is_dir() and _VERSION_DIR.fullmatch(item.name)
    ]
    if not candidates:
        return None
    if expected_schema:
        expected_major = expected_schema.partition(".")[0]
        same_major = [item for item in candidates if item.name.partition(".")[0] == expected_major]
        if same_major:
            candidates = same_major
    return max(candidates, key=lambda item: (_version_key(item.name), item.stat().st_mtime))


def _runtime_gimp_facts(
    ctx: ExecutionContext,
    application_id: str,
    config_root: Path,
    *,
    refresh: bool = False,
    fallback_schema: str = "",
) -> dict[str, object]:
    stored = None if refresh else load_flatpak_facts(ctx.root, application_id)
    if stored and bool(stored.get("installed")):
        return stored
    try:
        facts = inspect_flatpak_application(application_id)
    except Exception:  # noqa: BLE001 - el diagnóstico final será explícito
        facts = FlatpakApplicationFacts(application_id=application_id, installed=False)
    schema = facts.config_schema
    if not schema and fallback_schema and _VERSION_DIR.fullmatch(fallback_schema):
        schema = fallback_schema
    existing = None if schema else _detect_gimp_version_dir(config_root)
    if not schema and existing is not None:
        schema = existing.name
    payload = facts.to_dict()
    payload["config_schema"] = schema
    payload.update({
        "config_root": str(config_root),
        "config_path": str(config_root / schema) if schema else "",
    })
    if facts.installed and schema:
        normalized = FlatpakApplicationFacts(
            application_id=facts.application_id,
            installed=facts.installed,
            version=facts.version,
            branch=facts.branch,
            architecture=facts.architecture,
            origin=facts.origin,
            installation=facts.installation,
            ref=facts.ref,
            commit=facts.commit,
            config_schema=schema,
            observed_at=facts.observed_at,
        )
        save_flatpak_facts(
            ctx.root,
            normalized,
            config_root=str(config_root),
            config_path=payload["config_path"],
        )
    return payload


def _discover_gimp_config_path(config_root: Path, expected_schema: str) -> Path | None:
    """Busca el esquema esperado en las ubicaciones privadas de la app.

    La ruta declarada por el proveedor sigue siendo la primera opción. Si una
    versión de GIMP cambia mayúsculas o mueve la carpeta dentro del sandbox,
    Styler descubre el directorio real sin salir del árbol de la aplicación.
    """
    try:
        direct = _detect_gimp_version_dir(config_root, expected_schema)
    except TypeError:
        # Compatibilidad con extensiones/pruebas antiguas que implementaban
        # el detector con un único argumento.
        direct = _detect_gimp_version_dir(config_root)
    if direct is not None:
        return direct
    if not expected_schema:
        return None
    try:
        app_root = config_root.parents[1]
    except IndexError:
        return None
    if not app_root.is_dir():
        return None
    candidates: list[Path] = []
    for item in app_root.rglob(expected_schema):
        try:
            relative = item.relative_to(app_root)
        except ValueError:
            continue
        if len(relative.parts) > 6 or not item.is_dir():
            continue
        if any(part.casefold() == "gimp" for part in item.parent.parts):
            candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def _gimp_config_path_for_step(
    step: StepDefinition,
    ctx: ExecutionContext,
    raw_root: str,
) -> tuple[Path, dict[str, object], Path]:
    config_root = expand_user_path(raw_root, home=_home_of(ctx))
    app_id = str(step.config.get("runtime_facts_application_id") or "org.gimp.GIMP")
    facts = _runtime_gimp_facts(ctx, app_id, config_root)
    schema = str(facts.get("config_schema") or "")
    discovered = _discover_gimp_config_path(config_root, schema)
    config_path = discovered or (config_root / schema if schema else config_root)
    return config_root, facts, config_path


def _persist_resolved_gimp_path(
    ctx: ExecutionContext,
    facts: dict[str, object],
    *,
    config_root: Path,
    config_path: Path,
    **extra: object,
) -> None:
    """Actualiza el hecho persistido sin perder evidencia de ciclos previos."""
    try:
        normalized = FlatpakApplicationFacts.from_dict(
            {
                **facts,
                "installed": True,
                "config_schema": config_path.name,
            }
        )
        if not normalized.application_id:
            return
        base_fields = set(FlatpakApplicationFacts.__dataclass_fields__)
        preserved = {
            key: value
            for key, value in facts.items()
            if key not in base_fields and key not in {"config_root", "config_path"}
        }
        preserved.update(extra)
        save_flatpak_facts(
            ctx.root,
            normalized,
            **preserved,
            config_root=str(config_root),
            config_path=str(config_path),
        )
    except (OSError, TypeError, ValueError):
        # El paso principal conserva su resultado. La siguiente reconciliación
        # volverá a descubrir la ruta y podrá reconstruir este archivo.
        return


class ResolveFlatpakAppFactsExecutor(StepExecutor):
    """Resuelve versión/rama/ref después de instalar y antes de inicializar."""

    @property
    def step_type(self) -> str:
        return "resolve_flatpak_app_facts"

    def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        app_id = str(step.config.get("application_id") or "")
        raw_root = str(step.config.get("config_root") or "")
        if not app_id or not raw_root:
            return None
        stored = load_flatpak_facts(ctx.root, app_id)
        if stored and bool(stored.get("installed")) and stored.get("config_schema"):
            current = inspect_flatpak_application(app_id)
            if current.installed and (
                str(stored.get("version") or "") == current.version
                and str(stored.get("ref") or "") == current.ref
            ):
                return StepResult(
                    step.id,
                    step.step_type,
                    True,
                    Status.RECONCILED,
                    f"Versión de GIMP ya resuelta: {current.version} → configuración {current.config_schema}.",
                    data={**stored, "reconciled": True, "evidence": "flatpak+stored-facts"},
                )

        # Si no hay hechos o la versión/ref ya no coincide, este paso debe
        # ejecutarse otra vez. Es una consulta de solo lectura y evita que una
        # carpeta vieja (por ejemplo 3.2) oculte una actualización futura a 4.0.
        return None


    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        app_id = str(step.config.get("application_id") or "")
        raw_root = str(step.config.get("config_root") or "")
        if not app_id or not raw_root:
            return StepResult.failed(
                step, "Falta application_id o config_root para resolver la versión.",
                "FLATPAK_FACTS_CONFIG_ERROR",
            )
        try:
            config_root = expand_user_path(raw_root, home=_home_of(ctx))
        except PathResolutionError as exc:
            return StepResult.failed(step, str(exc), "UNSAFE_TARGET_PATH")
        if ctx.dry_run:
            return StepResult(
                step.id, step.step_type, True, Status.DRY_RUN,
                f"Se consultaría la versión instalada de {app_id} y se derivaría su carpeta de configuración.",
                data={"application_id": app_id, "config_root": str(config_root)},
            )
        facts = inspect_flatpak_application(app_id)
        if not facts.installed:
            return StepResult.failed(
                step, f"{app_id} no aparece instalado después del paso de instalación.",
                "FLATPAK_APP_NOT_INSTALLED",
            )
        if not facts.version or not facts.config_schema:
            return StepResult.failed(
                step,
                f"Flatpak no reportó una versión utilizable para {app_id}.",
                "FLATPAK_VERSION_UNRESOLVED",
                "Ejecuta `flatpak list --app --columns=application,version,branch` para revisar la instalación.",
            )
        config_path = config_root / facts.config_schema
        facts_file = save_flatpak_facts(
            ctx.root,
            facts,
            config_root=str(config_root),
            config_path=str(config_path),
        )
        return StepResult(
            step.id,
            step.step_type,
            True,
            Status.OK,
            f"GIMP {facts.version} usa el esquema de configuración {facts.config_schema}.",
            data={
                **facts.to_dict(),
                "config_root": str(config_root),
                "config_path": str(config_path),
                "facts_file": str(facts_file),
            },
        )


class InitializeFlatpakAppExecutor(StepExecutor):
    """Inicializa una aplicación Flatpak usando señales observables reales.

    Para GIMP no basta con detectar una carpeta versionada: esa carpeta
    puede crearse cuando la interfaz todavía está cargando recursos. El flujo
    correcto es:

    1. abrir la aplicación;
    2. esperar a que la aplicación publique una señal de disponibilidad
       (ventana, aplicación GTK registrada o instancia estable);
    3. solicitar un cierre normal mediante la acción GTK ``quit``;
    4. esperar a que la instancia desaparezca;
    5. esperar la carpeta correspondiente a la versión detectada creada al cerrar;
    6. esperar a que el árbol de configuración deje de recibir escrituras.

    Solo después puede comenzar el respaldo y la copia de PhotoGIMP.
    """

    @property
    def step_type(self) -> str:
        return "initialize_flatpak_app"

    @staticmethod
    def _run_flatpak_ps(columns: str) -> CommandResult | None:
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        try:
            return _run_probe(
                ["flatpak", "ps", f"--columns={columns}"],
                timeout=5,
                env=env,
            )
        except (OSError, TypeError):
            return None

    @staticmethod
    def _row_for_app(output: str, app_id: str) -> list[str] | None:
        for raw in output.splitlines():
            columns = raw.strip().split()
            if columns and columns[0] == app_id:
                return columns
        return None

    @classmethod
    def _flatpak_state(cls, app_id: str) -> tuple[bool, bool, str]:
        """Devuelve ``(running, active_window, diagnostic)``.

        ``flatpak ps`` es preferible a inspeccionar el PID del wrapper porque
        describe la instancia sandbox real. La columna ``active`` indica foco y ``background`` permite distinguir una instancia
        sin ventanas de una ventana abierta que temporalmente perdió el foco.
        """
        # Primero se pregunta únicamente por ``application``. Esa columna está
        # disponible en versiones antiguas de Flatpak. En 0.5.4 se hacía una
        # única consulta que incluía ``background``; cuando la versión local no
        # conocía esa columna, Styler concluía erróneamente que la aplicación no
        # estaba ejecutándose y ni siquiera podía cerrarla tras un timeout.
        base_probe = cls._run_flatpak_ps("application")
        if base_probe is None:
            return False, False, "flatpak ps no está disponible"
        if base_probe.returncode != 0:
            detail = (base_probe.stderr or base_probe.stdout or "sin detalle").strip()
            return False, False, f"flatpak ps(application) rc={base_probe.returncode}: {detail}"
        base_row = cls._row_for_app(base_probe.stdout, app_id)
        if base_row is None:
            return False, False, f"{app_id} no aparece en flatpak ps(application)"

        diagnostics = [f"running=True row={base_row!r}"]
        true_tokens = {"1", "active", "true", "yes", "y"}
        false_tokens = {"0", "false", "no", "n"}

        # Se intenta obtener señales de ventana con degradación compatible:
        # ``background`` es reciente; ``active`` existe en más instalaciones.
        for requested in ("application,active,background", "application,active"):
            probe = cls._run_flatpak_ps(requested)
            if probe is None:
                diagnostics.append(f"{requested}: no disponible")
                continue
            if probe.returncode != 0:
                detail = (probe.stderr or probe.stdout or "sin detalle").strip()
                diagnostics.append(f"{requested}: rc={probe.returncode} {detail}")
                continue
            columns = cls._row_for_app(probe.stdout, app_id)
            if columns is None:
                diagnostics.append(f"{requested}: aplicación ausente en la consulta enriquecida")
                continue
            active_token = columns[1].lower() if len(columns) > 1 else ""
            background_token = columns[2].lower() if len(columns) > 2 else ""
            active = active_token in true_tokens
            # `active` depende del foco. `background=no` aporta una señal más
            # estable: la instancia tiene una ventana abierta aunque otra
            # ventana gane el foco durante el cambio splash -> ventana principal.
            has_open_window = active or background_token in false_tokens
            diagnostics.append(
                f"{requested}: row={columns!r}; active={active_token!r}; "
                f"background={background_token!r}; window={has_open_window}"
            )
            return True, has_open_window, "; ".join(diagnostics)
        # La instancia sí existe aunque esta versión de Flatpak no pueda decir
        # si tiene ventana. Otros backends de readiness decidirán ese hito.
        return True, False, "; ".join(diagnostics)

    @staticmethod
    def _gtk_application_ready(app_id: str) -> tuple[bool, str]:
        """Comprueba si la aplicación publicó su objeto GTK en D-Bus.

        Es una señal semántica mejor que un ``sleep``: cuando el objeto existe,
        la aplicación ya registró su bucle principal y normalmente puede
        aceptar la acción ``quit``. No todas las aplicaciones lo publican, por
        eso se combina con ventana e instancia estable.
        """
        if not shutil.which("gdbus"):
            return False, "gdbus no está disponible"
        object_path = "/" + app_id.replace(".", "/")
        observations: list[str] = []
        for candidate in (object_path, f"{object_path}/Actions"):
            try:
                probe = _run_probe(
                    [
                        "gdbus", "introspect", "--session",
                        "--dest", app_id,
                        "--object-path", candidate,
                    ],
                    timeout=3,
                )
            except (OSError, TypeError) as exc:
                observations.append(f"{candidate}: {exc}")
                continue
            detail = (probe.stderr or probe.stdout or "sin detalle").strip()
            observations.append(f"{candidate}: rc={probe.returncode} {detail[:120]}")
            if probe.returncode == 0:
                return True, "; ".join(observations)
        return False, "; ".join(observations)

    @staticmethod
    def _request_graceful_quit(
        app_id: str,
        ctx: ExecutionContext | None = None,
        step: StepDefinition | None = None,
    ) -> tuple[bool, str]:
        """Intenta cerrar la aplicación por una vía que permita guardar estado.

        GIMP puede publicar nombres de acción distintos según la versión. Se
        prueban varias acciones GTK y después un cierre de ventana del gestor
        gráfico. Solo si todo falla el executor recurre a terminación forzada.
        """
        object_path = "/" + app_id.replace(".", "/")
        diagnostics: list[str] = []

        def execute(argv: list[str], label: str):
            if ctx is not None and step is not None:
                return run_step_command(ctx, step, argv, timeout=5, label=label)
            return _run_probe(argv, timeout=5)

        if shutil.which("gdbus"):
            for candidate in (object_path, f"{object_path}/Actions"):
                for action_name in ("quit", "file-quit", "app.quit"):
                    try:
                        result = execute(
                            [
                                "gdbus", "call", "--session",
                                "--dest", app_id,
                                "--object-path", candidate,
                                "--method", "org.gtk.Actions.Activate",
                                action_name, "[]", "{}",
                            ],
                            f"Solicitando el cierre de {app_id} mediante GTK",
                        )
                    except (OSError, TypeError) as exc:
                        diagnostics.append(f"{candidate}/{action_name}: {exc}")
                        continue
                    if result.returncode == 0:
                        return True, f"acción GTK {action_name} enviada en {candidate}"
                    detail = (result.stderr or result.stdout or "sin detalle").strip()
                    diagnostics.append(
                        f"{candidate}/{action_name}: rc={result.returncode} {detail[:160]}"
                    )
        else:
            diagnostics.append("gdbus no está disponible")

        # ``wmctrl -c`` envía WM_DELETE_WINDOW, equivalente a pulsar el botón
        # cerrar. Es preferible a matar el sandbox porque permite que GIMP
        # escriba sessionrc, menurc y el resto de la configuración inicial.
        if shutil.which("wmctrl"):
            for title in ("GIMP", "GNU Image Manipulation Program"):
                try:
                    result = execute(
                        ["wmctrl", "-c", title],
                        f"Solicitando el cierre de la ventana {title}",
                    )
                except (OSError, TypeError) as exc:
                    diagnostics.append(f"wmctrl {title!r}: {exc}")
                    continue
                if result.returncode == 0:
                    return True, f"WM_DELETE_WINDOW enviado a una ventana que coincide con {title!r}"
                detail = (result.stderr or result.stdout or "sin detalle").strip()
                diagnostics.append(f"wmctrl {title!r}: rc={result.returncode} {detail[:160]}")

        if shutil.which("xdotool"):
            for selector in ("--class", "--name"):
                try:
                    result = execute(
                        ["xdotool", "search", selector, "GIMP", "windowclose", "%@"],
                        f"Solicitando el cierre de GIMP mediante xdotool {selector}",
                    )
                except (OSError, TypeError) as exc:
                    diagnostics.append(f"xdotool {selector}: {exc}")
                    continue
                if result.returncode == 0:
                    return True, f"windowclose enviado mediante xdotool {selector} GIMP"
                detail = (result.stderr or result.stdout or "sin detalle").strip()
                diagnostics.append(f"xdotool {selector}: rc={result.returncode} {detail[:160]}")

        return False, "; ".join(diagnostics)

    @classmethod
    def _force_stop(
        cls,
        process: object | None,
        app_id: str,
        ctx: ExecutionContext | None = None,
        step: StepDefinition | None = None,
    ) -> str:
        diagnostics: list[str] = []
        if process is not None and process.poll() is None:
            PipeCraftRunner.stop_process(process, grace=8)
            diagnostics.append("PipeCraft detuvo el proceso de lanzamiento y su observación")

        running, _, detail = cls._flatpak_state(app_id)
        if running:
            try:
                result = (
                    run_step_command(
                        ctx, step, ["flatpak", "kill", app_id], timeout=8,
                        label=f"Forzando el cierre de {app_id}",
                    )
                    if ctx is not None and step is not None
                    else _run_probe(["flatpak", "kill", app_id], timeout=8)
                )
                diagnostics.append(
                    f"flatpak kill rc={result.returncode}: "
                    f"{(result.stderr or result.stdout or '').strip()}"
                )
            except (OSError, TimeoutError) as exc:
                diagnostics.append(f"flatpak kill falló: {exc}")
        else:
            diagnostics.append(detail)
        return "; ".join(diagnostics)

    def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        app_id = str(step.config.get("application_id") or "")
        raw_root = str(step.config.get("config_root") or "")
        if not app_id or not raw_root or not shutil.which("flatpak"):
            return None
        try:
            config_root = expand_user_path(raw_root, home=_home_of(ctx))
        except PathResolutionError:
            return None
        facts = _runtime_gimp_facts(ctx, app_id, config_root)
        expected_schema = str(facts.get("config_schema") or "")
        version_dir = _discover_gimp_config_path(config_root, expected_schema)
        if version_dir is None:
            return None
        running, _, detail = self._flatpak_state(app_id)
        if running:
            return None

        # Una carpeta existente no demuestra que GIMP completó un ciclo de
        # apertura/cierre controlado. Las versiones anteriores podían omitir
        # la inicialización por un directorio parcial. Solo reconciliamos con
        # evidencia persistida para la versión y ruta actuales.
        initialized = bool(facts.get("initialization_completed"))
        initialized_schema = str(facts.get("initialized_config_schema") or "")
        initialized_path = str(facts.get("initialized_config_path") or "")
        initialized_version = str(facts.get("initialized_application_version") or "")
        current_version = str(facts.get("version") or "")
        if not initialized:
            return None
        if initialized_schema != version_dir.name or initialized_path != str(version_dir):
            return None
        if initialized_version and current_version and initialized_version != current_version:
            return None

        _persist_resolved_gimp_path(
            ctx,
            facts,
            config_root=config_root,
            config_path=version_dir,
        )
        return StepResult(
            step.id,
            step.step_type,
            True,
            Status.RECONCILED,
            (
                f"GIMP {version_dir.name} ya completó un ciclo controlado de apertura y cierre; "
                "la evidencia coincide con la instalación actual."
            ),
            data={
                "application_id": app_id,
                "config_root": str(config_root),
                "config_path": str(version_dir),
                "config_version": version_dir.name,
                "reconciled": True,
                "evidence": "initialization-facts+filesystem+flatpak-state",
                "flatpak_state": detail,
                "flatpak_version": str(facts.get("version") or ""),
                "flatpak_branch": str(facts.get("branch") or ""),
                "expected_config_schema": expected_schema,
            },
        )

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        app_id = str(step.config.get("application_id") or "")
        raw_root = str(step.config.get("config_root") or "")
        try:
            startup_timeout = max(0.0, float(step.config.get("startup_timeout_seconds", 90)))
            poll_interval = max(0.01, float(step.config.get("poll_interval_seconds", 0.5)))
            window_stable = max(0.0, float(step.config.get("window_stable_seconds", 1.5)))
            running_stable_fallback = max(
                0.0,
                float(step.config.get("running_stable_fallback_seconds", 8)),
            )
            shutdown_timeout = max(0.0, float(step.config.get("shutdown_timeout_seconds", 20)))
            config_creation_timeout = max(
                0.0,
                float(step.config.get("config_creation_timeout_seconds", 30)),
            )
            config_creation_max = max(
                config_creation_timeout,
                float(step.config.get("config_creation_max_seconds", 600)),
            )
            config_flush_timeout = max(0.0, float(step.config.get("config_flush_timeout_seconds", 20)))
            config_flush_max = max(
                config_flush_timeout,
                float(step.config.get("config_flush_max_seconds", 600)),
            )
            config_quiet = max(0.0, float(step.config.get("config_quiet_seconds", 2)))
        except (TypeError, ValueError) as exc:
            return StepResult.failed(
                step,
                f"Configuración de espera inválida: {exc}",
                "INITIALIZATION_WAIT_CONFIG_ERROR",
            )
        if not app_id or not raw_root:
            return StepResult.failed(step, "Falta application_id o config_root.", "INITIALIZATION_CONFIG_ERROR")
        if ctx.dry_run:
            return StepResult(
                step.id,
                step.step_type,
                True,
                Status.DRY_RUN,
                (
                    f"Se abriría {app_id}; se esperaría una ventana activa estable "
                    f"durante {window_stable:g} s; se cerraría normalmente y se "
                    f"esperaría {config_quiet:g} s sin escrituras en la configuración."
                ),
                data={
                    "startup_timeout_seconds": startup_timeout,
                    "window_stable_seconds": window_stable,
                    "running_stable_fallback_seconds": running_stable_fallback,
                    "shutdown_timeout_seconds": shutdown_timeout,
                    "config_creation_timeout_seconds": config_creation_timeout,
                    "config_creation_max_seconds": config_creation_max,
                    "config_flush_timeout_seconds": config_flush_timeout,
                    "config_flush_max_seconds": config_flush_max,
                    "config_quiet_seconds": config_quiet,
                },
            )
        if not shutil.which("flatpak"):
            return StepResult.failed(step, "Flatpak no está disponible.", "FLATPAK_NOT_FOUND")
        try:
            config_root = expand_user_path(raw_root, home=_home_of(ctx))
        except PathResolutionError as exc:
            return StepResult.failed(step, str(exc), "UNSAFE_TARGET_PATH")
        already_running, _, already_detail = self._flatpak_state(app_id)
        if already_running:
            return StepResult(
                step.id, step.step_type, False, Status.FAILED,
                f"{app_id} ya estaba abierto antes de iniciar el paso.",
                data={
                    "error_code": "APP_ALREADY_RUNNING",
                    "hint": "Cierra GIMP y vuelve a ejecutar el plan para que Styler controle todo el ciclo.",
                    "flatpak_state": already_detail,
                },
            )

        facts = _runtime_gimp_facts(
            ctx,
            app_id,
            config_root,
            fallback_schema=str(step.config.get("expected_config_schema") or ""),
        )
        expected_schema = str(facts.get("config_schema") or "")
        installed_version = str(facts.get("version") or "")
        if not expected_schema:
            return StepResult.failed(
                step,
                f"No se pudo derivar la carpeta de configuración desde la versión instalada de {app_id}.",
                "GIMP_CONFIG_SCHEMA_UNRESOLVED",
                "Styler necesita que Flatpak reporte la versión antes de abrir GIMP.",
            )
        expected_config_path = config_root / expected_schema
        preexisting_version_dir = _discover_gimp_config_path(config_root, expected_schema)

        process_holder: dict[str, object] = {}
        version_holder: dict[str, Path] = {}
        active_since: dict[str, float | None] = {"value": None}
        running_since: dict[str, float | None] = {"value": None}
        state_detail: dict[str, str] = {"value": "sin consultar"}
        gtk_detail: dict[str, str] = {"value": "sin consultar"}
        started = time.monotonic()

        def launch(_context: ActionContext) -> ActionResult:
            try:
                process = start_step_process(
                    ctx,
                    step,
                    ["flatpak", "run", app_id],
                    label=f"Abriendo {app_id}",
                    env=os.environ.copy(),
                )
            except OSError as exc:
                return ActionResult(False, f"No se pudo iniciar {app_id}: {exc}")
            process_holder["process"] = process
            return ActionResult(True, f"{app_id} inició con PID {process.pid}.", {"pid": process.pid})

        def process_is_alive() -> bool:
            process = process_holder.get("process")
            if process is None:
                return False
            code = process.poll()
            if code is not None:
                # Algunos Flatpak delegan la aplicación a una instancia ya
                # registrada y el wrapper termina. En ese caso flatpak ps sigue
                # siendo la fuente de verdad.
                running, _, detail = self._flatpak_state(app_id)
                state_detail["value"] = detail
                if not running:
                    raise ConditionAborted(
                        f"{app_id} terminó con código {code} antes de mostrar una ventana."
                    )
            return True

        def active_window_is_stable() -> bool:
            running, active, detail = self._flatpak_state(app_id)
            state_detail["value"] = detail
            if not running or not active:
                active_since["value"] = None
                return False
            now = time.monotonic()
            if active_since["value"] is None:
                active_since["value"] = now
            return now - float(active_since["value"]) >= window_stable

        def active_window_diagnostic() -> str:
            since = active_since["value"]
            stable = 0.0 if since is None else max(0.0, time.monotonic() - float(since))
            return (
                f"active_stable={stable:.2f}/{window_stable:g}s; "
                f"flatpak={state_detail['value']}"
            )

        def gtk_application_is_ready() -> bool:
            ready, detail = self._gtk_application_ready(app_id)
            gtk_detail["value"] = detail
            return ready

        def gtk_application_diagnostic() -> str:
            return gtk_detail["value"]

        def running_instance_is_stable() -> bool:
            running, _, detail = self._flatpak_state(app_id)
            state_detail["value"] = detail
            if not running:
                running_since["value"] = None
                return False
            now = time.monotonic()
            if running_since["value"] is None:
                running_since["value"] = now
            return now - float(running_since["value"]) >= running_stable_fallback

        def running_instance_diagnostic() -> str:
            since = running_since["value"]
            stable = 0.0 if since is None else max(0.0, time.monotonic() - float(since))
            return (
                f"running_stable={stable:.2f}/{running_stable_fallback:g}s; "
                f"flatpak={state_detail['value']}"
            )

        def version_directory_exists() -> bool:
            version_dir = _discover_gimp_config_path(config_root, expected_schema)
            if version_dir is None:
                return False
            version_holder["path"] = version_dir
            return True

        def version_diagnostic() -> str:
            version = version_holder.get("path")
            return (
                f"flatpak_version={installed_version or 'no reportada'}; "
                f"expected_schema={expected_schema}; expected_path={expected_config_path}; "
                f"detected={version.name if version else 'no detectada'}"
            )

        def preexisting_config_is_usable() -> bool:
            if preexisting_version_dir is None:
                return False
            running, _, detail = self._flatpak_state(app_id)
            state_detail["value"] = detail
            if not running:
                return False
            version_holder["path"] = preexisting_version_dir
            return True

        def preexisting_config_diagnostic() -> str:
            return (
                f"preexisting_version="
                f"{preexisting_version_dir.name if preexisting_version_dir else 'no detectada'}"
            )

        profile = ApplicationProfile(
            id=app_id,
            process_name=app_id,
            process_condition=CallableCondition(
                f"proceso {app_id} activo",
                process_is_alive,
                lambda: f"pid={getattr(process_holder.get('process'), 'pid', None)}",
            ),
            readiness_checks=(
                LatchedCondition(
                    AnyCondition(
                        "GIMP publicó una señal de disponibilidad",
                        (
                            CallableCondition(
                                "GIMP mostró una ventana estable",
                                active_window_is_stable,
                                active_window_diagnostic,
                            ),
                            CallableCondition(
                                f"GIMP ya tenía configuración {expected_schema} antes de esta apertura",
                                preexisting_config_is_usable,
                                preexisting_config_diagnostic,
                            ),
                            CallableCondition(
                                "GIMP registró su aplicación GTK en D-Bus",
                                gtk_application_is_ready,
                                gtk_application_diagnostic,
                            ),
                            CallableCondition(
                                "La instancia de GIMP permaneció estable",
                                running_instance_is_stable,
                                running_instance_diagnostic,
                            ),
                        ),
                    ),
                    name="Styler ya observó GIMP utilizable",
                ),
            ),
            startup_timeout_seconds=startup_timeout,
            poll_interval_seconds=poll_interval,
            settle_seconds=0,
            minimum_runtime_seconds=0,
        )
        controller = ApplicationController(source=app_id)

        def on_poll(attempt: int, elapsed: float, diagnostic: str) -> None:
            progress = min(0.70, elapsed / startup_timeout * 0.70) if startup_timeout > 0 else None
            emit_step_progress(
                ctx,
                step,
                progress,
                f"Esperando la ventana completamente inicializada de GIMP · {elapsed:.1f} s",
                message=diagnostic,
            )

        emit_step_progress(ctx, step, 0.02, f"Abriendo {app_id}…")
        report = controller.launch_wait_and_settle(
            FunctionAction(f"abrir {app_id}", launch),
            profile,
            ActionContext(dry_run=False, variables=ctx.values, workdir=ctx.root),
            on_poll=on_poll,
        )
        process = process_holder.get("process")
        if not report.success:
            stop_diagnostic = self._force_stop(process, app_id, ctx, step)
            readiness_data = dict(report.readiness.data) if report.readiness else {}
            reason = readiness_data.get("reason")
            error_code = {
                "timeout": "APP_READY_TIMEOUT",
                "aborted": "APP_EXITED_BEFORE_READY",
                "error": "APP_READINESS_CHECK_FAILED",
            }.get(str(reason), "APP_INITIALIZATION_FAILED")
            message = report.readiness.message if report.readiness else report.launch.message
            diagnostic_path = ""
            if report.readiness is not None:
                try:
                    bundle = capture_wait_failure(
                        WaitResult(
                            satisfied=False,
                            condition=str(readiness_data.get("condition") or profile.id),
                            elapsed_seconds=float(readiness_data.get("elapsed_seconds") or 0.0),
                            attempts=int(readiness_data.get("attempts") or 0),
                            diagnostic=str(readiness_data.get("diagnostic") or ""),
                            reason=str(reason or "timeout"),
                        ),
                        root=ctx.root,
                        scope=step.id,
                        condition=profile.fully_loaded_condition(),
                        observed_paths=[config_root],
                        extra={
                            "application_id": app_id,
                            "startup_timeout_seconds": startup_timeout,
                            "window_stable_seconds": window_stable,
                            "lifecycle_state": report.final_state.value,
                            "stop_diagnostic": stop_diagnostic,
                        },
                    )
                    diagnostic_path = bundle.location
                    message = bundle.summary
                except OSError:
                    pass
            return StepResult(
                step.id,
                step.step_type,
                False,
                Status.TIMEOUT if reason == "timeout" else Status.FAILED,
                message,
                data={
                    "error_code": error_code,
                    "application_id": app_id,
                    "lifecycle_state": report.final_state.value,
                    "readiness": readiness_data,
                    "diagnostic_path": diagnostic_path,
                    "stop_diagnostic": stop_diagnostic,
                },
            )

        emit_step_progress(ctx, step, 0.74, "GIMP está listo; solicitando un cierre normal…")
        graceful, graceful_detail = self._request_graceful_quit(app_id, ctx, step)

        def app_has_stopped() -> bool:
            running, _, detail = self._flatpak_state(app_id)
            state_detail["value"] = detail
            if not running:
                return True
            process = process_holder.get("process")
            return process is not None and process.poll() is not None and not running

        shutdown = wait_until(
            CallableCondition(
                f"{app_id} se cerró",
                app_has_stopped,
                lambda: state_detail["value"],
            ),
            timeout_seconds=shutdown_timeout if graceful else 0,
            poll_interval_seconds=poll_interval,
        )
        forced_detail = ""
        if not shutdown.satisfied:
            emit_step_progress(ctx, step, 0.80, "El cierre normal no respondió; aplicando cierre de respaldo…")
            forced_detail = self._force_stop(process, app_id, ctx, step)
            shutdown = wait_until(
                CallableCondition(
                    f"{app_id} dejó de ejecutarse",
                    app_has_stopped,
                    lambda: state_detail["value"],
                ),
                timeout_seconds=max(3.0, min(10.0, shutdown_timeout)),
                poll_interval_seconds=poll_interval,
            )
        if not shutdown.satisfied:
            return StepResult(
                step.id, step.step_type, False, Status.TIMEOUT,
                f"GIMP no terminó después de solicitar su cierre: {shutdown.diagnostic}",
                data={
                    "error_code": "APP_SHUTDOWN_TIMEOUT",
                    "graceful_quit": graceful,
                    "graceful_detail": graceful_detail,
                    "forced_stop": forced_detail,
                },
            )

        controller.bus.publish(Event(EventType.STOP_REQUESTED, app_id))
        controller.bus.publish(Event(EventType.APP_STOPPED, app_id))
        observed_process = process_holder.get("process")
        finish_observation = getattr(observed_process, "finish_observation", None)
        if callable(finish_observation):
            finish_observation(getattr(observed_process, "returncode", None))

        # GIMP puede materializar la carpeta versionada durante el cierre. La
        # versión anterior esperaba esa carpeta antes de solicitar ``quit`` y
        # formaba un interbloqueo: Styler no cerraba hasta verla y GIMP no la
        # terminaba de crear hasta cerrar.
        emit_step_progress(ctx, step, 0.84, "Esperando la configuración creada durante el cierre…")
        config_activity = DirectoryQuiescentCondition(
            config_root, stable_for_seconds=0,
        )
        config_created = wait_until(
            CallableCondition(
                f"GIMP creó la carpeta de configuración esperada {expected_schema}",
                version_directory_exists,
                version_diagnostic,
                activity=config_activity.activity_token,
            ),
            timeout_seconds=config_creation_max,
            inactivity_timeout_seconds=config_creation_timeout,
            poll_interval_seconds=poll_interval,
        )
        if not config_created.satisfied:
            return StepResult(
                step.id,
                step.step_type,
                False,
                Status.TIMEOUT,
                (
                    f"GIMP {installed_version or '(versión no reportada)'} cerró, pero no apareció "
                    f"la carpeta esperada {expected_config_path}: "
                    f"{config_created.diagnostic}"
                ),
                data={
                    "error_code": "GIMP_CONFIG_NOT_CREATED",
                    "config_root": str(config_root),
                    "expected_config_path": str(expected_config_path),
                    "flatpak_version": installed_version,
                    "flatpak_branch": str(facts.get("branch") or ""),
                    "expected_config_schema": expected_schema,
                    "config_creation": config_created.__dict__,
                    "graceful_quit": graceful,
                    "graceful_detail": graceful_detail,
                    "forced_stop": forced_detail,
                },
            )

        version_dir = version_holder.get("path") or _discover_gimp_config_path(
            config_root, expected_schema
        )
        if version_dir is None:
            return StepResult.failed(
                step,
                f"La espera detectó configuración, pero no pudo resolver {expected_config_path}.",
                "GIMP_CONFIG_NOT_CREATED",
            )

        emit_step_progress(ctx, step, 0.90, "Esperando a que GIMP termine de guardar su configuración…")
        flush = wait_until(
            DirectoryQuiescentCondition(
                version_dir,
                stable_for_seconds=config_quiet,
            ),
            timeout_seconds=config_flush_max,
            inactivity_timeout_seconds=config_flush_timeout,
            poll_interval_seconds=poll_interval,
        )
        if not flush.satisfied:
            return StepResult(
                step.id, step.step_type, False, Status.TIMEOUT,
                f"GIMP se cerró, pero su configuración no se estabilizó: {flush.diagnostic}",
                data={
                    "error_code": "GIMP_CONFIG_FLUSH_TIMEOUT",
                    "config_path": str(version_dir),
                    "shutdown": shutdown.__dict__,
                    "config_flush": flush.__dict__,
                },
            )

        _persist_resolved_gimp_path(
            ctx,
            facts,
            config_root=config_root,
            config_path=version_dir,
            initialization_completed=True,
            initialized_at=time.time(),
            initialized_run_id=ctx.run_id,
            initialized_application_version=installed_version,
            initialized_config_schema=version_dir.name,
            initialized_config_path=str(version_dir),
            initialized_gracefully=bool(graceful and not forced_detail),
            initialization_graceful_detail=graceful_detail,
            initialization_forced_stop=forced_detail,
        )

        emit_step_progress(ctx, step, 1.0, f"GIMP {version_dir.name} cerró y terminó de guardar su configuración.")
        return StepResult(
            step.id,
            step.step_type,
            True,
            Status.OK,
            f"GIMP quedó inicializado, cerrado y con su configuración {version_dir.name} estable.",
            data={
                "application_id": app_id,
                "flatpak_version": installed_version,
                "flatpak_branch": str(facts.get("branch") or ""),
                "flatpak_ref": str(facts.get("ref") or ""),
                "expected_config_schema": expected_schema,
                "config_version": version_dir.name,
                "config_path": str(version_dir),
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "startup_timeout_seconds": startup_timeout,
                "window_stable_seconds": window_stable,
                "running_stable_fallback_seconds": running_stable_fallback,
                "shutdown_timeout_seconds": shutdown_timeout,
                "config_creation_timeout_seconds": config_creation_timeout,
                "config_creation_max_seconds": config_creation_max,
                "config_flush_timeout_seconds": config_flush_timeout,
                "config_flush_max_seconds": config_flush_max,
                "config_quiet_seconds": config_quiet,
                "graceful_quit": graceful,
                "graceful_detail": graceful_detail,
                "forced_stop": forced_detail,
                "readiness": dict(report.readiness.data) if report.readiness else {},
                "shutdown": shutdown.__dict__,
                "config_creation": config_created.__dict__,
                "config_flush": flush.__dict__,
                "lifecycle_state": controller.machine.state.value,
                "lifecycle_history": [
                    {
                        "previous": item.previous.value,
                        "event": item.event.value,
                        "current": item.current.value,
                    }
                    for item in controller.machine.history
                ],
            },
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

        from styler.runtime.executors import PackageInstallExecutor

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


PHOTOGIMP_RELEASE_PREFIX = "https://github.com/Diolinux/PhotoGIMP/releases/"
PHOTOGIMP_DOWNLOAD_PREFIX = "https://github.com/Diolinux/PhotoGIMP/releases/download/"
PHOTOGIMP_LATEST_API = "https://api.github.com/repos/Diolinux/PhotoGIMP/releases/latest"


def _styler_user_agent() -> str:
    try:
        from styler import __version__

        return f"Styler-Reinvented/{__version__}"
    except Exception:  # noqa: BLE001 - la descarga no depende de conocer la versión
        return "Styler-Reinvented"


def _looks_like_linux_asset(name: str) -> bool:
    """Acepta únicamente un activo que se identifique explícitamente como Linux.

    Un ZIP genérico puede contener otra distribución de carpetas o pertenecer a
    otra plataforma. Si la release vigente no publica un activo Linux, Styler
    conserva la URL Linux fijada por el catálogo en vez de adivinar.
    """
    lowered = name.lower()
    if not lowered.endswith(".zip") or "linux" not in lowered:
        return False
    if any(token in lowered for token in ("windows", "macos", "mac-", "osx")):
        return False
    return True


def resolve_photogimp_source(pinned: str, *, opener=None) -> tuple[str, dict[str, object]]:
    """Resuelve la publicación vigente de PhotoGIMP, no una versión congelada.

    El instructivo oficial dice «descarga la última release». Fijar una etiqueta
    deja a Styler atrás en cada publicación: la 3.1, por ejemplo, mejoró la
    portabilidad del lanzador Flatpak entre distribuciones y arquitecturas.

    Solo se acepta una URL bajo el prefijo oficial de descargas del repositorio.
    Si la API no responde, se conserva la fuente declarada en el catálogo: el
    cambio sigue siendo instalable sin depender de un servicio externo.
    """
    evidence: dict[str, object] = {"pinned_source": pinned, "resolution": "pinned"}
    opener = opener or urlopen
    request = Request(
        PHOTOGIMP_LATEST_API,
        headers={
            "User-Agent": _styler_user_agent(),
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with opener(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        evidence["resolution_error"] = f"{type(exc).__name__}: {exc}"
        return pinned, evidence

    if not isinstance(payload, dict):
        evidence["resolution_error"] = "La API no devolvió un objeto de publicación."
        return pinned, evidence

    tag = str(payload.get("tag_name") or "")
    assets = payload.get("assets")
    candidates: list[str] = []
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            url = str(asset.get("browser_download_url") or "")
            name = str(asset.get("name") or "")
            if url.startswith(PHOTOGIMP_DOWNLOAD_PREFIX) and _looks_like_linux_asset(name):
                candidates.append(url)

    if not candidates:
        evidence["resolution_error"] = (
            f"La publicación {tag or 'más reciente'} no expone un ZIP de Linux verificable."
        )
        return pinned, evidence

    # Se prefiere el activo que se nombra explícitamente para Linux.
    chosen = next((url for url in candidates if "linux" in url.lower()), candidates[0])
    evidence.update({
        "resolution": "latest-release",
        "release_tag": tag,
        "resolved_source": chosen,
    })
    return chosen, evidence


def _safe_extract_zip(archive: Path, destination: Path) -> list[str]:
    """Extrae un ZIP sin permitir rutas absolutas ni escapes con ``..``."""
    extracted: list[str] = []
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                raise ValueError(f"Ruta insegura dentro del ZIP: {info.filename}")
            target = destination.joinpath(*member.parts).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(f"Ruta fuera del HOME dentro del ZIP: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            extracted.append(str(target))
    return extracted


def _desktop_value(path: Path, key: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    prefix = f"{key}="
    for line in lines:
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            return value or None
    return None


def _installed_flatpak_desktop(home: Path, app_id: str) -> Path | None:
    """Localiza el lanzador exportado por la instalación Flatpak real."""
    candidates = (
        home / ".local/share/flatpak/exports/share/applications" / f"{app_id}.desktop",
        Path("/var/lib/flatpak/exports/share/applications") / f"{app_id}.desktop",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _rewrite_photogimp_desktop(
    path: Path,
    *,
    config_version: str,
    home: Path,
    app_id: str = "org.gimp.GIMP",
) -> dict[str, str]:
    """Adapta el lanzador a la instalación Flatpak y versión detectadas.

    ``StartupWMClass`` no admite comodines como ``gimp-3.x``. Por eso se
    toma primero el valor del lanzador exportado por Flatpak y, cuando no está
    disponible, se construye a partir de la carpeta real de configuración.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}

    official = _installed_flatpak_desktop(home, app_id)
    official_exec = _desktop_value(official, "Exec") if official else None
    startup_wm_class = _desktop_value(official, "StartupWMClass") if official else None
    if not startup_wm_class:
        startup_wm_class = f"gimp-{config_version}"
    exec_line = official_exec or f"flatpak run {app_id} %U"

    rewritten: list[str] = []
    seen: set[str] = set()
    replacements = {
        "Exec": exec_line,
        "TryExec": "flatpak",
        "X-Flatpak": app_id,
        "StartupWMClass": startup_wm_class,
    }
    for line in lines:
        key, separator, _ = line.partition("=")
        if separator and key in replacements:
            rewritten.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            rewritten.append(line)
    for key, value in replacements.items():
        if key not in seen:
            rewritten.append(f"{key}={value}")
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    return {
        "exec": exec_line,
        "startup_wm_class": startup_wm_class,
        "flatpak_desktop": str(official) if official else "",
    }


@dataclass
class CopyEffects:
    created_paths: list[str] = field(default_factory=list)
    created_directories: list[str] = field(default_factory=list)
    overwritten: list[dict[str, str]] = field(default_factory=list)
    verified_files: list[dict[str, str]] = field(default_factory=list)

    def merge(self, other: "CopyEffects") -> None:
        self.created_paths.extend(other.created_paths)
        self.created_directories.extend(other.created_directories)
        self.overwritten.extend(other.overwritten)
        self.verified_files.extend(other.verified_files)

    @property
    def touched(self) -> list[str]:
        return [*self.created_paths, *(item["path"] for item in self.overwritten)]

    def receipt_data(self, *, target: Path, partial: bool = False) -> dict[str, object]:
        return {
            "created_paths": list(self.created_paths),
            "created_directories": list(self.created_directories),
            "overwritten": [dict(item) for item in self.overwritten],
            "verified_files": [dict(item) for item in self.verified_files],
            "target": str(target),
            "partial": partial,
        }


def _assert_no_symlink_components(path: Path, boundary: Path) -> None:
    """Rechaza redirecciones mediante symlinks dentro del árbol administrado.

    ``expand_user_path`` confina la raíz al HOME, pero un enlace simbólico ya
    existente *dentro* del destino podría desviar una escritura posterior.
    Esta comprobación conserva la semántica del overlay sin seguir enlaces
    creados por terceros.
    """
    try:
        path.relative_to(boundary)
    except ValueError as exc:
        raise ValueError(f"El destino {path} queda fuera de {boundary}.") from exc

    current = path
    while True:
        if current.is_symlink():
            raise ValueError(f"Styler no escribe a través de enlaces simbólicos: {current}")
        if current == boundary:
            break
        current = current.parent


def _ensure_directory(
    path: Path,
    effects: CopyEffects,
    *,
    boundary: Path | None = None,
) -> None:
    if boundary is not None:
        _assert_no_symlink_components(path, boundary)
    elif path.is_symlink():
        raise ValueError(f"Styler no crea directorios a través de enlaces simbólicos: {path}")

    missing: list[Path] = []
    current = path
    while not current.exists():
        if current.is_symlink():
            raise ValueError(f"Styler no crea directorios a través de enlaces simbólicos: {current}")
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    path.mkdir(parents=True, exist_ok=True)
    effects.created_directories.extend(str(item) for item in reversed(missing))


def _backup_existing_file(target: Path, backup_root: Path) -> Path:
    if target.is_symlink():
        raise ValueError(f"Styler no respalda ni sobrescribe enlaces simbólicos: {target}")
    relative = Path(*target.parts[1:]) if target.is_absolute() else target
    backup = backup_root / relative
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, backup)
    return backup


def _copy_files_individually(
    source: Path,
    destination: Path,
    *,
    backup_root: Path,
    effects: CopyEffects | None = None,
) -> CopyEffects:
    """Fusiona un árbol copiando y verificando cada archivo por separado.

    La carpeta fuente nunca se renombra ni sustituye la carpeta destino. Cada
    archivo regular se mapea por su ruta relativa, se respalda si ya existía,
    se copia y se verifica inmediatamente mediante SHA-256. Los elementos que
    solo existen en el destino se conservan.
    """
    if not source.is_dir():
        raise ValueError(f"El origen del overlay no es un directorio: {source}")

    effects = effects or CopyEffects()
    _ensure_directory(destination, effects, boundary=destination)

    entries = sorted(
        source.rglob("*"),
        key=lambda item: item.relative_to(source).as_posix(),
    )
    for item in entries:
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_symlink():
            raise ValueError(f"No se copian enlaces simbólicos desde overlays: {item}")
        _assert_no_symlink_components(target, destination)

        if item.is_dir():
            if target.exists() and not target.is_dir():
                raise ValueError(f"El destino {target} no es un directorio.")
            _ensure_directory(target, effects, boundary=destination)
            continue
        if not item.is_file():
            raise ValueError(f"PhotoGIMP contiene un elemento no regular: {item}")
        if target.exists() and target.is_dir():
            raise ValueError(f"El destino {target} es un directorio y no puede sobrescribirse.")

        _ensure_directory(target.parent, effects, boundary=destination)
        if target.is_symlink():
            raise ValueError(f"El destino {target} es un enlace simbólico.")

        operation = "created"
        backup = ""
        if target.exists():
            operation = "replaced"
            backup_path = _backup_existing_file(target, backup_root)
            backup = str(backup_path)
            effects.overwritten.append({"path": str(target), "backup": backup})
        else:
            effects.created_paths.append(str(target))

        expected_hash = _sha256_file(item)
        shutil.copy2(item, target)
        actual_hash = _sha256_file(target)
        if actual_hash != expected_hash:
            raise ValueError(
                f"La verificación SHA-256 falló al copiar {relative.as_posix()}."
            )
        effects.verified_files.append({
            "source": str(item),
            "destination": str(target),
            "relative_path": relative.as_posix(),
            "operation": operation,
            "backup": backup,
            "sha256": actual_hash,
        })

    return effects


def _copy_tree_contents(
    source: Path,
    destination: Path,
    *,
    backup_root: Path,
    effects: CopyEffects | None = None,
) -> CopyEffects:
    """Compatibilidad: la copia de árboles ahora siempre es archivo por archivo."""
    return _copy_files_individually(
        source,
        destination,
        backup_root=backup_root,
        effects=effects,
    )


# Raíces que PhotoGIMP ha publicado a lo largo de sus versiones. ``.var`` es la
# estructura de las publicaciones antiguas para Flatpak y ``.icons`` acompañaba
# al lanzador personalizado. Se reconocen todas para que una publicación futura
# no quede a medio aplicar en silencio.
PHOTOGIMP_PAYLOAD_ROOTS = (".config", ".local", ".icons", ".var")


def _payload_depth(path: Path, extracted_root: Path) -> int:
    try:
        return len(path.relative_to(extracted_root).parts)
    except ValueError:
        return 999


def _find_photogimp_payload(extracted_root: Path) -> Path:
    """Localiza recursivamente la raíz del paquete, incluso con carpetas ocultas.

    La instalación localiza directamente una estructura funcional de PhotoGIMP.
    Primero se exige ``.config/GIMP/<versión>`` o el layout de configuración
    Flatpak. Solo si no existe se acepta una raíz que contenga recursos.
    """
    candidates = [extracted_root]
    candidates.extend(
        item for item in extracted_root.rglob("*")
        if item.is_dir() and _payload_depth(item, extracted_root) <= 6
    )

    functional: list[Path] = []
    resources_only: list[Path] = []
    for root in candidates:
        modern = root / ".config" / "GIMP"
        flatpak_layout = root / ".var" / "app" / "org.gimp.GIMP" / "config" / "GIMP"
        if any(
            tree.is_dir() and any(
                child.is_dir() and _VERSION_DIR.fullmatch(child.name)
                for child in tree.iterdir()
            )
            for tree in (modern, flatpak_layout)
        ):
            functional.append(root)
        elif any((root / name).is_dir() for name in PHOTOGIMP_PAYLOAD_ROOTS):
            resources_only.append(root)

    if functional:
        return min(functional, key=lambda item: (_payload_depth(item, extracted_root), str(item)))
    if resources_only:
        return min(resources_only, key=lambda item: (_payload_depth(item, extracted_root), str(item)))
    raise ValueError(
        "El ZIP no contiene una plantilla .config/GIMP/<versión> ni recursos PhotoGIMP reconocibles."
    )


def _photogimp_template_roots(extracted_root: Path) -> list[Path]:
    """Encuentra directamente todos los árboles GIMP versionados del ZIP.

    No depende del nombre o profundidad de la carpeta contenedora y pathlib sí
    incluye nombres ocultos como ``.config`` y ``.var``.
    """
    roots: list[tuple[int, int, str, Path]] = []
    for candidate in [extracted_root, *extracted_root.rglob("GIMP")]:
        if not candidate.is_dir() or candidate.name != "GIMP":
            continue
        versions = [
            child for child in candidate.iterdir()
            if child.is_dir() and _VERSION_DIR.fullmatch(child.name)
        ]
        if not versions:
            continue
        parts = candidate.parts
        modern = candidate.parent.name == ".config"
        flatpak_layout = ".var" in parts and "org.gimp.GIMP" in parts
        kind_priority = 0 if modern else 1 if flatpak_layout else 2
        roots.append((kind_priority, _payload_depth(candidate, extracted_root), str(candidate), candidate))

    roots.sort(key=lambda item: item[:3])
    return [item[3] for item in roots]


def _select_photogimp_template_from_archive(
    extracted_root: Path,
    target_schema: str,
) -> tuple[Path, Path, str, Path]:
    """Selecciona árbol, versión fuente, adaptación y raíz asociada del payload."""
    errors: list[str] = []
    for template_root in _photogimp_template_roots(extracted_root):
        try:
            source_version, mode = _select_photogimp_source_version(
                template_root,
                target_schema,
            )
        except ValueError as exc:
            errors.append(f"{template_root}: {exc}")
            continue

        payload = template_root
        for ancestor in template_root.parents:
            if ancestor.name in {".config", ".var"}:
                payload = ancestor.parent
                break
            if ancestor == extracted_root:
                payload = extracted_root
                break
        return template_root, source_version, mode, payload

    detail = "; ".join(errors[:5])
    suffix = f" Detalle: {detail}" if detail else ""
    raise ValueError(
        f"No se encontró una plantilla PhotoGIMP compatible con GIMP {target_schema}." + suffix
    )


def _photogimp_template_root(payload: Path) -> Path:
    """Localiza un árbol de plantillas bajo una raíz conocida.

    Esta función permanece para compatibilidad. La instalación productiva usa
    ``_select_photogimp_template_from_archive`` y busca directamente en todo el ZIP.
    """
    roots = _photogimp_template_roots(payload)
    if roots:
        return roots[0]
    raise ValueError(
        "PhotoGIMP no contiene plantillas de configuración versionadas de GIMP."
    )


def _find_photogimp_resource_roots(
    extracted_root: Path,
    payload_hint: Path,
) -> dict[str, Path]:
    """Localiza por separado ``.local`` y ``.icons`` para copiarlos individualmente."""
    found: dict[str, Path] = {}
    for name in (".local", ".icons"):
        direct = payload_hint / name
        if direct.is_dir():
            found[name] = direct
            continue
        candidates = [
            item for item in extracted_root.rglob(name)
            if item.is_dir() and _payload_depth(item, extracted_root) <= 7
        ]
        if candidates:
            found[name] = min(
                candidates,
                key=lambda item: (_payload_depth(item, extracted_root), str(item)),
            )
    return found


def _install_photogimp_release(
    step: StepDefinition, ctx: ExecutionContext, source_uri: str, marker_target: Path
) -> StepResult:
    """Instala PhotoGIMP con recibos exactos y adaptación opcional."""
    if not source_uri.startswith(PHOTOGIMP_RELEASE_PREFIX):
        return StepResult.failed(step, "Fuente remota de PhotoGIMP no autorizada.", "UNTRUSTED_OVERLAY_SOURCE")
    if not shutil.which("flatpak"):
        return StepResult.failed(step, "PhotoGIMP requiere Flatpak.", "FLATPAK_NOT_FOUND")
    probe = run_step_command(
        ctx, step, ["flatpak", "info", "org.gimp.GIMP"], timeout=20,
        label="Comprobando la instalación de GIMP",
    )
    if probe.returncode != 0:
        return StepResult.failed(
            step, "PhotoGIMP requiere GIMP instalado como Flatpak.", "PHOTOGIMP_REQUIRES_GIMP_FLATPAK"
        )

    home = _home_of(ctx) or Path.home()
    home.mkdir(parents=True, exist_ok=True)
    facts = _runtime_gimp_facts(ctx, "org.gimp.GIMP", marker_target)
    installed_version = str(facts.get("version") or "")
    expected_schema = str(facts.get("config_schema") or "")
    if not expected_schema:
        return StepResult.failed(
            step,
            "No se pudo derivar el esquema de configuración desde la versión instalada de GIMP.",
            "GIMP_CONFIG_SCHEMA_UNRESOLVED",
        )
    version_target = _discover_gimp_config_path(marker_target, expected_schema)
    if version_target is None:
        # El overlay oficial se aplica bajo ~/.config/GIMP. Esa carpeta puede
        # no existir todavía cuando GIMP Flatpak conserva su estado inicial en
        # el sandbox. Tras demostrar un ciclo completo de apertura/cierre,
        # Styler puede crear el destino XDG y aplicar allí la plantilla.
        version_target = marker_target / expected_schema

    if bool(step.config.get("require_initialized_cycle", False)):
        initialized_path = str(facts.get("initialized_config_path") or "")
        initialized_version = str(facts.get("initialized_application_version") or "")
        path_independent = bool(step.config.get("initialization_path_independent", False))
        if (
            not bool(facts.get("initialization_completed"))
            or (not path_independent and initialized_path != str(version_target))
            or (initialized_version and installed_version and initialized_version != installed_version)
        ):
            return StepResult.failed(
                step,
                "No existe evidencia válida de que esta versión de GIMP se abrió y cerró completamente.",
                "GIMP_INITIALIZATION_EVIDENCE_MISSING",
                "Ejecuta la fase de inicialización antes de aplicar PhotoGIMP.",
            )

    running, _, state_detail = InitializeFlatpakAppExecutor._flatpak_state("org.gimp.GIMP")
    if running:
        return StepResult.failed(
            step,
            "GIMP sigue abierto justo antes de aplicar PhotoGIMP; Styler no modificó archivos.",
            "APP_MUST_BE_CLOSED_FOR_OVERLAY",
            f"Cierra GIMP y continúa la integración. Estado detectado: {state_detail}",
        )

    required_backup_step_id = str(step.config.get("required_backup_step_id") or "")
    if required_backup_step_id:
        change_id = str(ctx.values.get("change_id") or "")
        if not change_id:
            return StepResult.failed(
                step,
                "No se puede demostrar el respaldo completo porque falta el identificador del cambio.",
                "REQUIRED_BACKUP_EVIDENCE_MISSING",
            )
        journal = ReceiptJournal(ctx.values.get("receipts_root") or ctx.root, change_id)
        backup_receipt = journal.latest_pending_for_step(
            required_backup_step_id,
            kind=ReceiptKind.BACKUP_CREATED,
        )
        backup_ok = backup_receipt is not None and str(backup_receipt.data.get("source") or "") == str(version_target)
        if backup_ok and bool(backup_receipt.data.get("existed")):
            backup_ok = bool(backup_receipt.data.get("backup")) and Path(
                str(backup_receipt.data.get("backup"))
            ).exists()
        if not backup_ok:
            return StepResult.failed(
                step,
                f"No hay un respaldo completo y vigente de {version_target}; Styler no aplicó PhotoGIMP.",
                "REQUIRED_BACKUP_EVIDENCE_MISSING",
                "Vuelve a ejecutar la fase de respaldo antes de copiar el overlay.",
            )

    try:
        ensure_receipts_writable(ctx)
    except ReceiptWriteError as exc:
        return StepResult.failed(
            step, str(exc), "RECEIPT_JOURNAL_UNAVAILABLE",
            "La instalación no comenzó porque Styler no podía garantizar Deshacer.",
        )

    source_uri, release_evidence = resolve_photogimp_source(source_uri)
    if not source_uri.startswith(PHOTOGIMP_RELEASE_PREFIX):
        return StepResult.failed(
            step,
            "La fuente resuelta de PhotoGIMP no pertenece al repositorio oficial.",
            "UNTRUSTED_OVERLAY_SOURCE",
        )
    request = Request(source_uri, headers={"User-Agent": _styler_user_agent()})
    effects = CopyEffects()
    backup_root = ctx.root / ".styler" / "write-backups" / (ctx.run_id or "pending") / step.id
    rewrite_launchers = bool(step.config.get("rewrite_launchers", True))
    launcher_metadata: dict[str, str] = {}
    source_version_name = ""
    adaptation_mode = ""
    config_manifest: dict[str, str] = {}
    manifest_mismatches: list[str] = []
    applied_roots: list[str] = []
    unapplied: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="styler-photogimp-") as temp_dir:
            temp = Path(temp_dir)
            archive = temp / "PhotoGIMP-linux.zip"
            extracted = temp / "extracted"
            extracted.mkdir()
            emit_step_progress(ctx, step, 0.03, "Conectando con la publicación oficial de PhotoGIMP…")
            with urlopen(request, timeout=45) as response, archive.open("wb") as output:
                total_bytes = int(getattr(response, "headers", {}).get("Content-Length") or 0)
                downloaded = 0
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
                    if total_bytes:
                        fraction = downloaded / total_bytes
                        emit_step_progress(
                            ctx, step, 0.05 + fraction * 0.45,
                            f"Descargando PhotoGIMP · {downloaded / 1048576:.1f} de {total_bytes / 1048576:.1f} MB",
                        )
                    else:
                        emit_step_progress(
                            ctx, step, None,
                            f"Descargando PhotoGIMP · {downloaded / 1048576:.1f} MB recibidos",
                        )
            emit_step_progress(ctx, step, 0.52, "Validando el archivo descargado…")
            if not zipfile.is_zipfile(archive):
                return StepResult.failed(step, "GitHub no entregó un ZIP válido de PhotoGIMP.", "INVALID_OVERLAY_ARCHIVE")
            _safe_extract_zip(archive, extracted)
            emit_step_progress(ctx, step, 0.62, "Localizando la plantilla de configuración archivo por archivo…")
            source_root, source_version, adaptation_mode, payload = (
                _select_photogimp_template_from_archive(
                    extracted,
                    version_target.name,
                )
            )
            source_version_name = source_version.name
            config_manifest = _tree_manifest(source_version)
            if not config_manifest:
                raise ValueError(
                    f"La plantilla PhotoGIMP {source_version_name} no contiene archivos para aplicar."
                )

            # La descarga puede tardar. Se vuelve a comprobar el estado
            # inmediatamente antes de la primera escritura para impedir que
            # GIMP sobrescriba la configuración al cerrar.
            running_now, _, current_state = InitializeFlatpakAppExecutor._flatpak_state(
                "org.gimp.GIMP"
            )
            if running_now:
                raise ValueError(
                    "GIMP se abrió mientras PhotoGIMP se descargaba; no se copiaron archivos. "
                    f"Estado detectado: {current_state}"
                )

            # Se copian los contenidos de la plantilla sobre la carpeta real
            # creada por GIMP. Nunca se renombra 3.0 a 3.2/4.0 ni se elimina la
            # configuración original completa: solo se sustituyen los elementos
            # incluidos por PhotoGIMP y se conservan los demás.
            emit_step_progress(
                ctx,
                step,
                0.72,
                (
                    f"Aplicando plantilla PhotoGIMP {source_version_name} sobre "
                    f"GIMP {version_target.name} ({adaptation_mode})…"
                ),
            )
            _copy_files_individually(
                source_version,
                version_target,
                backup_root=backup_root / "gimp",
                effects=effects,
            )
            if bool(step.config.get("verify_overlay_manifest", True)):
                manifest_mismatches = _verify_overlay_manifest(config_manifest, version_target)
                if manifest_mismatches:
                    preview = ", ".join(manifest_mismatches[:8])
                    raise ValueError(
                        f"La copia de configuración no coincide con la plantilla PhotoGIMP: {preview}"
                    )

            # El instructivo oficial extrae el ZIP completo sobre el HOME. Se
            # replica ese efecto para todas las carpetas de recursos que la
            # publicación traiga, no solo .local: el icono y el lanzador viven
            # en .icons en varias publicaciones.
            resource_roots = _find_photogimp_resource_roots(extracted, payload)
            for resource_name, resource_source in resource_roots.items():
                emit_step_progress(
                    ctx,
                    step,
                    0.84,
                    f"Copiando cada archivo de {resource_name} a su ruta de usuario…",
                )
                _copy_files_individually(
                    resource_source,
                    home / resource_name,
                    backup_root=backup_root / resource_name.lstrip("."),
                    effects=effects,
                )
                applied_roots.append(resource_name)

            unapplied = sorted(
                name for name in (".local", ".icons")
                if name not in resource_roots
            )

            if rewrite_launchers:
                emit_step_progress(ctx, step, 0.92, "Adaptando el acceso del menú para GIMP Flatpak…")
                desktop_root = home / ".local" / "share" / "applications"
                for raw in effects.touched:
                    desktop = Path(raw)
                    if desktop.suffix != ".desktop" or desktop_root not in desktop.parents:
                        continue
                    try:
                        content = desktop.read_text(encoding="utf-8").lower()
                    except (OSError, UnicodeDecodeError):
                        continue
                    if "photogimp" in desktop.name.lower() or "photogimp" in content:
                        launcher_metadata = _rewrite_photogimp_desktop(
                            desktop,
                            config_version=version_target.name,
                            home=home,
                        )
            else:
                emit_step_progress(ctx, step, 0.92, "Conservando los lanzadores originales de PhotoGIMP…")
    except (HTTPError, URLError, TimeoutError, OSError, zipfile.BadZipFile, ValueError) as exc:
        if effects.touched or effects.created_directories:
            emit_receipt(
                ctx, step, ReceiptKind.PATHS_WRITTEN,
                effects.receipt_data(target=version_target, partial=True),
            )
        return StepResult.failed(
            step,
            f"No se pudo descargar o aplicar PhotoGIMP desde GitHub: {exc}",
            "REMOTE_OVERLAY_INSTALL_FAILED",
            "Comprueba la conexión y conserva el reporte; los efectos parciales quedaron registrados.",
        )

    marker = marker_target / ".photogimp-marker"
    manifest_path = marker_target / ".photogimp-manifest.json"
    marker_effects = CopyEffects()
    try:
        _ensure_directory(marker.parent, marker_effects)
        if marker.is_symlink():
            raise ValueError(f"Styler no escribe marcadores a través de enlaces simbólicos: {marker}")
        if marker.exists():
            backup = _backup_existing_file(marker, backup_root / "marker")
            marker_effects.overwritten.append({"path": str(marker), "backup": str(backup)})
        else:
            marker_effects.created_paths.append(str(marker))
        marker.write_text(
            (
                f"source={source_uri}\n"
                f"provider=flatpak\n"
                f"source_config_version={source_version_name}\n"
                f"config_version={version_target.name}\n"
                f"adaptation_mode={adaptation_mode}\n"
                f"config_manifest_files={len(config_manifest)}\n"
                f"manifest=.photogimp-manifest.json\n"
                f"startup_wm_class={launcher_metadata.get('startup_wm_class', '')}\n"
                f"installed_at={int(time.time())}\n"
            ),
            encoding="utf-8",
        )

        if manifest_path.is_symlink():
            raise ValueError(
                f"Styler no escribe manifiestos a través de enlaces simbólicos: {manifest_path}"
            )
        if manifest_path.exists():
            backup = _backup_existing_file(manifest_path, backup_root / "marker")
            marker_effects.overwritten.append({"path": str(manifest_path), "backup": str(backup)})
        else:
            marker_effects.created_paths.append(str(manifest_path))
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": "styler.photogimp-overlay/1",
                    "source": source_uri,
                    "source_config_version": source_version_name,
                    "target_config_version": version_target.name,
                    "target_config_path": str(version_target),
                    "adaptation_mode": adaptation_mode,
                    "source_template_root": str(source_root),
                    "files": config_manifest,
                    "verified_files": [
                        item for item in effects.verified_files
                        if Path(item["destination"]).is_relative_to(version_target)
                    ],
                    "created_at": int(time.time()),
                },
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError) as exc:
        if effects.touched or effects.created_directories:
            emit_receipt(
                ctx, step, ReceiptKind.PATHS_WRITTEN,
                effects.receipt_data(target=version_target, partial=True),
            )
        return StepResult.failed(
            step, f"PhotoGIMP se copió, pero no pudo marcarse como instalado: {exc}",
            "OVERLAY_MARKER_FAILED"
        )

    emit_receipt(
        ctx, step, ReceiptKind.PATHS_WRITTEN,
        effects.receipt_data(target=version_target),
    )
    emit_receipt(
        ctx, step, ReceiptKind.MARKER_WRITTEN,
        marker_effects.receipt_data(target=marker_target),
    )

    applied = effects.touched
    return StepResult(
        step.id, step.step_type, True, Status.OK,
        f"PhotoGIMP se aplicó a GIMP Flatpak {version_target.name}; se tocaron {len(applied)} archivos.",
        output="\n".join(applied),
        data={
            "applied": applied,
            "created_paths": effects.created_paths,
            "overwritten": effects.overwritten,
            "target": str(version_target),
            "marker_target": str(marker_target),
            "source": source_uri,
            "source_config_version": source_version_name,
            "adaptation_mode": adaptation_mode,
            "config_manifest_files": len(config_manifest),
            "files_copied_individually": len(effects.verified_files),
            "verified_files": effects.verified_files,
            "source_template_root": str(source_root),
            "config_manifest_verified": not manifest_mismatches,
            "config_manifest_path": str(manifest_path),
            "flatpak_version": installed_version,
            "flatpak_branch": str(facts.get("branch") or ""),
            "expected_config_schema": expected_schema,
            "gimp_config_version": version_target.name,
            "rewrite_launchers": rewrite_launchers,
            "launcher": launcher_metadata.get("exec", "flatpak run org.gimp.GIMP %U") if rewrite_launchers else "sin adaptar",
            "startup_wm_class": launcher_metadata.get("startup_wm_class", ""),
            "flatpak_desktop": launcher_metadata.get("flatpak_desktop", ""),
            "applied_payload_roots": [".config", *applied_roots],
            "unapplied_payload_roots": unapplied,
            **release_evidence,
        },
    )


def _apply_overlay(step: StepDefinition, ctx: ExecutionContext, assets_root: Path | None = None) -> StepResult:
    """Copia un asset con recibos exactos por archivo."""
    source_uri = str(step.config.get("source", ""))
    raw_target = str(step.config.get("target", ""))

    if ctx.dry_run:
        return StepResult(
            step.id, step.step_type, True, Status.DRY_RUN,
            f"Se aplicaría {source_uri or '(sin fuente)'} sobre {raw_target or '(sin destino)' }.",
            data={"source": source_uri, "target": raw_target},
        )
    if not raw_target:
        return StepResult.failed(
            step, "El componente no declara una ruta destino real.", "NO_CONFIG_TARGET",
            "Declara [resources.paths], o el 'config_root' del proveedor del que depende.",
        )
    try:
        target = expand_user_path(raw_target, home=_home_of(ctx))
    except PathResolutionError as exc:
        return StepResult.failed(step, str(exc), "UNRESOLVED_SOURCE_OR_TARGET")

    if source_uri.startswith(PHOTOGIMP_RELEASE_PREFIX):
        return _install_photogimp_release(step, ctx, source_uri, target)
    try:
        if source_uri.startswith("package://"):
            content_root = Path(str(ctx.values.get("package_content_root") or "")).resolve()
            if not str(content_root) or not content_root.is_dir():
                raise PathResolutionError("El grafo portable no recibió package_content_root.")
            relative = source_uri[len("package://"):]
            if not relative or ".." in Path(relative).parts:
                raise PathResolutionError(f"Ruta package:// insegura: {source_uri}")
            asset = (content_root / relative).resolve()
            try:
                asset.relative_to(content_root)
            except ValueError as exc:
                raise PathResolutionError(f"El recurso escapa del paquete: {source_uri}") from exc
            if not asset.exists():
                raise PathResolutionError(f"No existe el recurso del paquete: {source_uri}")
        else:
            asset = resolve_catalog_uri(source_uri, assets_root=assets_root)
    except PathResolutionError as exc:
        return StepResult.failed(step, str(exc), "UNRESOLVED_SOURCE_OR_TARGET")
    try:
        ensure_receipts_writable(ctx)
    except ReceiptWriteError as exc:
        return StepResult.failed(step, str(exc), "RECEIPT_JOURNAL_UNAVAILABLE")

    effects = CopyEffects()
    backup_root = ctx.root / ".styler" / "write-backups" / (ctx.run_id or "pending") / step.id
    try:
        _copy_tree_contents(asset, target, backup_root=backup_root, effects=effects)
        modes = step.config.get("modes") or {}
        if isinstance(modes, dict):
            for relative, raw_mode in modes.items():
                candidate = (target / str(relative)).resolve()
                try:
                    candidate.relative_to(target.resolve())
                except ValueError as exc:
                    raise ValueError(f"Modo fuera del destino: {relative}") from exc
                if candidate.exists() and not candidate.is_symlink():
                    candidate.chmod(int(raw_mode))
    except (OSError, ValueError) as exc:
        if effects.touched or effects.created_directories:
            emit_receipt(
                ctx, step, ReceiptKind.PATHS_WRITTEN,
                effects.receipt_data(target=target, partial=True),
            )
        return StepResult.failed(step, f"No se pudo aplicar el overlay: {exc}", "OVERLAY_APPLY_FAILED")

    emit_receipt(
        ctx, step, ReceiptKind.PATHS_WRITTEN, effects.receipt_data(target=target)
    )
    applied = effects.touched
    return StepResult(
        step.id, step.step_type, True, Status.OK,
        f"Se aplicaron {len(applied)} archivos sobre {target}.",
        output="\n".join(applied),
        data={
            "applied": applied,
            "created_paths": effects.created_paths,
            "overwritten": effects.overwritten,
            "target": str(target),
            "source": source_uri,
        },
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
        from styler.runtime.executors import PackageInstallExecutor
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
    ya existente (``styler/pipelines.py``), que trabaja desde el almacén de
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
    """``ExecutorRegistry.default()`` más los ejecutores del catálogo."""
    registry = ExecutorRegistry.default()
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
