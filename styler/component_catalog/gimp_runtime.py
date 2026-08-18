"""Resolución e inicialización de GIMP/Flatpak para cambios de catálogo.

Se mantiene separado de los ejecutores genéricos porque contiene el ciclo de
vida específico de GIMP y la observación de su sandbox Flatpak.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from pathlib import Path, PurePosixPath

from styler.automation.actions import ActionContext, ActionResult, FunctionAction
from styler.automation.conditions import (
    AnyCondition, CallableCondition, ConditionAborted, DirectoryQuiescentCondition,
    LatchedCondition, WaitResult, wait_until,
)
from styler.automation.controller import ApplicationController
from styler.automation.diagnostics import capture_wait_failure
from styler.automation.events import Event, EventType
from styler.automation.profiles import ApplicationProfile
from styler.component_catalog.paths import PathResolutionError, expand_user_path
from styler.flatpak_facts import (
    FlatpakApplicationFacts, inspect_flatpak_application, load_flatpak_facts, save_flatpak_facts,
)
from styler.execution.processes import (
    CommandResult, ProcessRunner, run_step_command, start_step_process,
)
from styler.execution.base import StepExecutor, emit_step_progress
from styler.planning.models import ExecutionContext, Status, StepDefinition, StepResult

from .executor_utils import _home_of, _run_probe

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
        # disponible en versiones antiguas de Flatpak. Antes se hacía una
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
            ProcessRunner.stop_process(process, grace=8)
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
