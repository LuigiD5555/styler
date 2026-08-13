"""Primitivas declarativas para artefactos de release y AppImage.

No contienen conocimiento de Affinity. Los YAML incorporados deciden qué
release descargar y cómo componer estas acciones en un DAG.
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import shlex
import shutil
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from styler.applications import apt_install_argv
from styler.receipts import ReceiptKind, ReceiptWriteError, emit_receipt, ensure_receipts_writable
from styler.runtime.commands import PipeCraftRunner, run_step_command
from styler.runtime.executors import PackageInstallExecutor, StepExecutor, emit_step_progress
from styler.runtime.models import ExecutionContext, Status, StepDefinition, StepResult


def _home(ctx: ExecutionContext) -> Path:
    return Path(ctx.values.get("home") or Path.home()).expanduser()


def artifact_path(ctx: ExecutionContext, artifact_id: str, filename: str) -> Path:
    safe_id = "".join(ch for ch in artifact_id if ch.isalnum() or ch in "._-").strip("._-")
    safe_name = Path(filename).name
    if not safe_id or safe_name != filename or safe_name in {"", ".", ".."}:
        raise ValueError("Referencia de artefacto insegura.")
    return ctx.root / ".styler" / "downloads" / safe_id / safe_name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _satisfied_by_existing_capability(config: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Evalúa una condición declarativa que permite reutilizar infraestructura existente.

    El YAML puede declarar::

        satisfied_by:
          executable: ail-cli

    No es una excepción para AppImageLauncher: cualquier primitive que use este
    helper puede reconciliarse contra una capacidad ya presente sin descargar ni
    reinstalar nada.
    """
    raw = config.get("satisfied_by")
    if not isinstance(raw, dict):
        return False, "", {}
    executable = str(raw.get("executable") or "").strip()
    if executable:
        path = shutil.which(executable)
        if path:
            return True, f"{executable} ya está disponible; no hace falta descargar ni reinstalar su proveedor.", {
                "executable": executable,
                "path": path,
                "satisfied_by": "executable",
            }
    return False, "", {}


def _resolve_release_url(config: dict[str, object]) -> str:
    direct = str(config.get("url") or "")
    if direct:
        return direct
    if str(config.get("source") or "") != "github":
        raise ValueError("La fuente de release no está soportada.")
    repository = str(config.get("repository") or "")
    tag = str(config.get("tag") or "")
    asset = str(config.get("asset") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Repositorio GitHub inválido.")
    if not tag or not asset or Path(asset).name != asset:
        raise ValueError("Tag o asset GitHub inválido.")
    owner, repo = repository.split("/", 1)
    from urllib.parse import quote
    api_url = (
        f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/releases/tags/{quote(tag, safe='')}"
    )
    request = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Styler/0.9.10",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    for raw in payload.get("assets") or []:
        if str(raw.get("name") or "") != asset:
            continue
        url = str(raw.get("browser_download_url") or "")
        parsed = urlparse(url)
        expected = f"/{repository}/releases/download/{tag}/".lower()
        if parsed.scheme == "https" and parsed.netloc.lower() in {"github.com", "www.github.com"} and parsed.path.lower().startswith(expected):
            return url
    raise ValueError(f"La release {repository}@{tag} no publica el asset {asset}.")


class ReleaseFetchExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "fetch_release_artifact"

    def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        config = dict(step.config)
        satisfied, message, data = _satisfied_by_existing_capability(config)
        if satisfied:
            return StepResult(
                step.id, step.step_type, True, Status.RECONCILED, message,
                data={**data, "download_skipped": True, "reconciled": True},
            )
        try:
            path = artifact_path(ctx, str(config.get("artifact_id") or ""), str(config.get("filename") or ""))
        except ValueError:
            return None
        expected = str(config.get("sha256") or "").lower()
        if not path.is_file():
            return None
        if expected and _sha256(path) != expected:
            return None
        return StepResult(
            step.id, step.step_type, True, Status.RECONCILED,
            f"El artefacto {path.name} ya está descargado y reutilizable.",
            data={"path": str(path), "sha256": _sha256(path), "reconciled": True},
        )

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        config = dict(step.config)
        satisfied, message, data = _satisfied_by_existing_capability(config)
        if satisfied:
            return StepResult(
                step.id, step.step_type, True, Status.RECONCILED, message,
                data={**data, "download_skipped": True, "reconciled": True},
            )
        artifact_id = str(config.get("artifact_id") or "")
        filename = str(config.get("filename") or config.get("asset") or "")
        expected = str(config.get("sha256") or "").lower()
        try:
            url = _resolve_release_url(config) if not ctx.dry_run else (
                str(config.get("url") or "") or
                f"github://{config.get('repository', '')}@{config.get('tag', '')}/{config.get('asset', '')}"
            )
        except Exception as exc:  # noqa: BLE001 - frontera de API de releases
            return StepResult.failed(step, f"No se pudo resolver la release: {exc}", "RELEASE_RESOLUTION_FAILED")
        if not ctx.dry_run:
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.netloc:
                return StepResult.failed(step, "La descarga necesita una URL HTTPS válida.", "RELEASE_URL_INVALID")
        try:
            destination = artifact_path(ctx, artifact_id, filename)
        except ValueError as exc:
            return StepResult.failed(step, str(exc), "RELEASE_ARTIFACT_INVALID")
        if ctx.dry_run:
            return StepResult(
                step.id, step.step_type, True, Status.DRY_RUN,
                f"Se descargaría {url} en la caché de Styler.",
                data={"url": url, "path": str(destination)},
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_suffix(destination.suffix + ".part")
        request = urllib.request.Request(url, headers={"User-Agent": "Styler/0.9.10"})
        emit_step_progress(ctx, step, 0.05, f"Descargando {filename}…")
        try:
            with urllib.request.urlopen(request, timeout=int(step.timeout or 300)) as response, temp.open("wb") as handle:
                total = int(response.headers.get("Content-Length") or 0)
                downloaded = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    progress = min(0.95, downloaded / total) if total else None
                    emit_step_progress(ctx, step, progress, f"Descargando {filename}…")
            digest = _sha256(temp)
            if expected and digest != expected:
                temp.unlink(missing_ok=True)
                return StepResult.failed(
                    step,
                    f"El SHA-256 de {filename} no coincide con el declarado.",
                    "RELEASE_CHECKSUM_MISMATCH",
                )
            os.replace(temp, destination)
        except Exception as exc:  # noqa: BLE001 - frontera de red
            temp.unlink(missing_ok=True)
            return StepResult.failed(step, f"No se pudo descargar {url}: {exc}", "RELEASE_DOWNLOAD_FAILED")
        emit_step_progress(ctx, step, 1.0, f"Descarga terminada: {destination.name}.")
        return StepResult(
            step.id, step.step_type, True, Status.OK,
            f"Artefacto descargado: {destination.name}.",
            data={"url": url, "path": str(destination), "sha256": _sha256(destination)},
        )


class PackageInstallArtifactExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "install_package_artifact"

    def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        config = dict(step.config)
        satisfied, message, data = _satisfied_by_existing_capability(config)
        if satisfied:
            return StepResult(
                step.id, step.step_type, True, Status.RECONCILED, message,
                data={**data, "install_skipped": True, "reconciled": True},
            )
        manager = str(config.get("manager") or "")
        package_name = str(config.get("package_name") or "")
        if manager and package_name and PackageInstallExecutor._is_installed(manager, package_name):
            return StepResult(
                step.id, step.step_type, True, Status.RECONCILED,
                f"{package_name} ya está instalado; se reutilizará.",
                data={"manager": manager, "package": package_name, "reconciled": True},
            )
        return None

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        config = dict(step.config)
        satisfied, message, data = _satisfied_by_existing_capability(config)
        if satisfied:
            return StepResult(
                step.id, step.step_type, True, Status.RECONCILED, message,
                data={**data, "install_skipped": True, "reconciled": True},
            )
        manager = str(config.get("manager") or "")
        artifact_id = str(config.get("artifact_id") or "")
        filename = str(config.get("filename") or "")
        package_name = str(config.get("package_name") or "")
        if manager != "apt":
            return StepResult.failed(step, f"Instalación de artefactos no soportada para {manager}.", "ARTIFACT_MANAGER_UNSUPPORTED")
        try:
            path = artifact_path(ctx, artifact_id, filename)
        except ValueError as exc:
            return StepResult.failed(step, str(exc), "ARTIFACT_REFERENCE_INVALID")
        if not path.is_file():
            return StepResult.failed(step, f"No existe el artefacto descargado: {path}", "ARTIFACT_NOT_FOUND")
        if not package_name:
            return StepResult.failed(step, "El YAML debe declarar package_name para una instalación reversible.", "ARTIFACT_PACKAGE_NAME_MISSING")
        if PackageInstallExecutor._is_installed(manager, package_name):
            return StepResult(
                step.id, step.step_type, True, Status.RECONCILED,
                f"{package_name} ya estaba instalado; no se modificó.",
                data={"manager": manager, "package": package_name, "already_present": True},
            )
        if ctx.dry_run:
            return StepResult(
                step.id, step.step_type, True, Status.DRY_RUN,
                f"Se instalaría {path.name} mediante APT.", data={"path": str(path)},
            )
        try:
            ensure_receipts_writable(ctx)
        except ReceiptWriteError as exc:
            return StepResult.failed(step, str(exc), "RECEIPT_JOURNAL_UNAVAILABLE")
        prefix = PackageInstallExecutor._privileged_prefix()
        if prefix is None or not shutil.which("apt-get"):
            return StepResult.failed(step, "APT o un mecanismo de privilegios no está disponible.", "APT_UNAVAILABLE")
        argv = apt_install_argv(prefix, str(path.resolve()))
        command = run_step_command(ctx, step, argv, timeout=step.timeout, label=f"Instalando {package_name} desde {path.name}")
        if command.returncode != 0:
            result = StepResult.failed(
                step,
                f"No se pudo instalar {package_name} desde {path.name} (código {command.returncode}).",
                "ARTIFACT_INSTALL_FAILED",
                command.stderr or command.stdout or f"Consulta {command.log_path}",
            )
            result.data.update({"artifact": command.log_path, "returncode": command.returncode})
            return result
        if not PackageInstallExecutor._is_installed(manager, package_name):
            return StepResult.failed(step, f"APT terminó sin error, pero {package_name} no aparece instalado.", "ARTIFACT_INSTALL_NOT_VERIFIED")
        if not bool(config.get("retain_on_rollback")):
            emit_receipt(ctx, step, ReceiptKind.PACKAGE_INSTALLED, {
                "manager": manager,
                "package": package_name,
                "was_present": False,
                "source_artifact": str(path),
            })
        return StepResult(
            step.id, step.step_type, True, Status.OK,
            f"Paquete instalado desde artefacto: {package_name}.",
            output=command.stdout,
            data={"artifact": command.log_path, "package": package_name, "manager": manager},
        )


class ExecutableVerifyExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "verify_executable"

    def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        executable = str(step.config.get("executable") or "")
        path = shutil.which(executable) if executable else None
        if not path:
            return None
        return StepResult(step.id, step.step_type, True, Status.RECONCILED, f"{executable} ya está disponible.", data={"path": path})

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        executable = str(step.config.get("executable") or "")
        path = shutil.which(executable) if executable else None
        if not path:
            return StepResult.failed(step, f"No se encontró el ejecutable {executable}.", "EXECUTABLE_NOT_FOUND")
        return StepResult(step.id, step.step_type, True, Status.OK, f"Ejecutable verificado: {path}.", data={"path": path})


def _snapshot_integration(home: Path) -> set[Path]:
    roots = (
        home / "Applications",
        home / ".local" / "share" / "applications",
        home / ".local" / "share" / "icons",
        home / ".icons",
    )
    files: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            files.update(item for item in root.rglob("*") if item.is_file() or item.is_symlink())
        except OSError:
            continue
    return files


def _matching_desktops(home: Path, hint: str) -> list[Path]:
    root = home / ".local" / "share" / "applications"
    if not root.is_dir():
        return []
    needle = hint.lower()
    matches: list[Path] = []
    for path in root.glob("*.desktop"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if needle in path.name.lower() or needle in text.lower():
            matches.append(path)
    return matches


def _exec_target(exec_line: str) -> Path | None:
    """Primer ejecutable/ruta real de una entrada Desktop Entry."""
    if not exec_line.strip():
        return None
    try:
        tokens = shlex.split(exec_line)
    except ValueError:
        return None
    for token in tokens:
        if not token or token.startswith("%") or "=" in token and not token.startswith("/"):
            continue
        if token.startswith("/"):
            return Path(token)
        break
    return None


def _icon_installed(home: Path, icon: str) -> bool:
    if not icon.strip():
        return False
    candidate = Path(icon).expanduser()
    if candidate.is_absolute():
        return candidate.is_file()
    roots = (
        home / ".local" / "share" / "icons",
        home / ".icons",
        Path("/usr/share/icons"),
        Path("/usr/share/pixmaps"),
    )
    names = {icon, f"{icon}.png", f"{icon}.svg", f"{icon}.xpm"}
    for root in roots:
        if not root.is_dir():
            continue
        for name in names:
            direct = root / name
            if direct.is_file():
                return True
        try:
            for path in root.rglob("*"):
                if path.is_file() and (path.name in names or path.stem == icon):
                    return True
        except OSError:
            continue
    return False


class AppImageIntegrateExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "integrate_appimage"

    def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        hint = str(step.config.get("name_hint") or Path(str(step.config.get("filename") or "")).stem)
        matches = _matching_desktops(_home(ctx), hint)
        if matches:
            return StepResult(
                step.id, step.step_type, True, Status.RECONCILED,
                f"{hint} ya tiene una entrada de escritorio integrada.",
                data={"desktop_entries": [str(item) for item in matches]},
            )
        return None

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        config = dict(step.config)
        artifact_id = str(config.get("artifact_id") or "")
        filename = str(config.get("filename") or "")
        hint = str(config.get("name_hint") or Path(filename).stem)
        backend = str(config.get("backend") or "appimagelauncher")
        if backend != "appimagelauncher":
            return StepResult.failed(step, f"Backend AppImage no soportado: {backend}", "APPIMAGE_BACKEND_UNSUPPORTED")
        ail = shutil.which("ail-cli")
        if not ail:
            return StepResult.failed(step, "AppImageLauncher no está listo: falta ail-cli.", "APPIMAGELAUNCHER_NOT_READY")
        try:
            source = artifact_path(ctx, artifact_id, filename)
        except ValueError as exc:
            return StepResult.failed(step, str(exc), "APPIMAGE_REFERENCE_INVALID")
        if not source.is_file():
            return StepResult.failed(step, f"No existe el AppImage descargado: {source}", "APPIMAGE_NOT_FOUND")
        if ctx.dry_run:
            return StepResult(
                step.id, step.step_type, True, Status.DRY_RUN,
                f"Se integraría {source.name} mediante AppImageLauncher.",
                data={"source": str(source), "backend": backend},
            )
        try:
            ensure_receipts_writable(ctx)
        except ReceiptWriteError as exc:
            return StepResult.failed(step, str(exc), "RECEIPT_JOURNAL_UNAVAILABLE")
        source.chmod(source.stat().st_mode | 0o111)
        home = _home(ctx)
        before = _snapshot_integration(home)
        command = run_step_command(
            ctx, step, [ail, "integrate", str(source)], timeout=step.timeout,
            label=f"Integrando {hint} con AppImageLauncher",
        )
        if command.returncode != 0:
            result = StepResult.failed(
                step,
                f"AppImageLauncher no pudo integrar {hint} (código {command.returncode}).",
                "APPIMAGE_INTEGRATION_FAILED",
                command.stderr or command.stdout or f"Consulta {command.log_path}",
            )
            result.data.update({"artifact": command.log_path, "returncode": command.returncode})
            return result
        after = _snapshot_integration(home)
        created_paths = set(after - before)
        desktop_paths = _matching_desktops(home, hint)
        # AppImageLauncher puede tener una carpeta de integración personalizada.
        # La ruta real queda escrita en Exec; la incluimos aunque esté fuera de
        # ~/Applications para que el recibo represente el efecto completo.
        for desktop in desktop_paths:
            try:
                for line in desktop.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("Exec="):
                        target = _exec_target(line.split("=", 1)[1])
                        if target is not None and target.is_file() and target not in before:
                            created_paths.add(target)
                        break
            except OSError:
                continue
        created = sorted(str(path) for path in created_paths)
        desktops = [str(path) for path in desktop_paths]
        if not desktops:
            result = StepResult.failed(
                step,
                "AppImageLauncher terminó sin error, pero Styler no encontró una entrada .desktop para la aplicación.",
                "APPIMAGE_DESKTOP_NOT_FOUND",
                f"Consulta {command.log_path}",
            )
            result.data.update({"created_paths": created, "artifact": command.log_path})
            return result
        try:
            emit_receipt(ctx, step, ReceiptKind.PATHS_WRITTEN, {
                "created_paths": created,
                "created_directories": [],
                "overwritten": [],
                "backend": backend,
                "desktop_entries": desktops,
            })
        except ReceiptWriteError as exc:
            return StepResult.failed(step, str(exc), "RECEIPT_WRITE_FAILED")
        return StepResult(
            step.id, step.step_type, True, Status.OK,
            f"{hint} quedó integrado al escritorio mediante AppImageLauncher.",
            output=command.stdout,
            data={"created_paths": created, "desktop_entries": desktops, "artifact": command.log_path},
        )


class AppImageVerifyExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "verify_appimage_integration"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        hint = str(step.config.get("name_hint") or "")
        home = _home(ctx)
        desktops = _matching_desktops(home, hint)
        if not desktops:
            return StepResult.failed(step, f"No existe una entrada .desktop integrada para {hint}.", "APPIMAGE_VERIFY_DESKTOP_MISSING")
        details: list[dict[str, str]] = []
        failures: list[str] = []
        for desktop in desktops:
            try:
                lines = desktop.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as exc:
                failures.append(f"{desktop}: {exc}")
                continue
            values: dict[str, str] = {}
            for line in lines:
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    values.setdefault(key.strip(), value.strip())
            exec_line = values.get("Exec", "")
            icon = values.get("Icon", "")
            if not exec_line:
                failures.append(f"{desktop.name}: falta Exec")
            elif ".AppImage" not in exec_line and "appimagelauncher" not in exec_line.lower():
                failures.append(f"{desktop.name}: Exec no apunta a una integración AppImage")
            else:
                target = _exec_target(exec_line)
                if target is not None and not target.is_file():
                    failures.append(f"{desktop.name}: el AppImage de Exec no existe: {target}")
            if not icon:
                failures.append(f"{desktop.name}: falta Icon")
            elif not _icon_installed(home, icon):
                failures.append(f"{desktop.name}: no se encontró el icono instalado «{icon}»")
            details.append({"desktop": str(desktop), "exec": exec_line, "icon": icon})
        if failures:
            return StepResult.failed(step, "La integración AppImage quedó incompleta: " + "; ".join(failures), "APPIMAGE_VERIFY_FAILED")
        return StepResult(
            step.id, step.step_type, True, Status.OK,
            f"Integración verificada: {len(desktops)} entrada(s) de escritorio para {hint}.",
            data={"desktop_entries": details},
        )


__all__ = [
    "AppImageIntegrateExecutor", "AppImageVerifyExecutor", "ExecutableVerifyExecutor",
    "PackageInstallArtifactExecutor", "ReleaseFetchExecutor", "artifact_path",
]
