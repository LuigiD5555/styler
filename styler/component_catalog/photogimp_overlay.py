"""Instalación segura del payload de PhotoGIMP.

Este módulo concentra descarga, selección de plantilla y copia reversible del
overlay. No participa en scheduling: PipeCraft sigue siendo el runtime.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from styler.component_catalog.paths import PathResolutionError, expand_user_path, resolve_catalog_uri
from styler.receipts import (
    ReceiptJournal, ReceiptKind, ReceiptWriteError, emit_receipt, ensure_receipts_writable,
)
from styler.execution.processes import run_step_command
from styler.execution.base import emit_step_progress
from styler.planning.models import ExecutionContext, Status, StepDefinition, StepResult

from .executor_utils import _home_of
from .gimp_runtime import (
    InitializeFlatpakAppExecutor, _VERSION_DIR, _discover_gimp_config_path,
    _runtime_gimp_facts, _select_photogimp_source_version, _sha256_file,
    _tree_manifest, _verify_overlay_manifest,
)

PHOTOGIMP_RELEASE_PREFIX = "https://github.com/Diolinux/PhotoGIMP/releases/"
PHOTOGIMP_DOWNLOAD_PREFIX = "https://github.com/Diolinux/PhotoGIMP/releases/download/"
PHOTOGIMP_LATEST_API = "https://api.github.com/repos/Diolinux/PhotoGIMP/releases/latest"


def _styler_user_agent() -> str:
    try:
        from styler import __version__

        return f"Styler/{__version__}"
    except Exception:  # noqa: BLE001 - la descarga no depende de conocer la versión
        return "Styler"


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
