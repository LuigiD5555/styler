"""Ejecutores conservadores del DAG de reversión.

Una reversión nunca borra recursivamente un directorio preexistente. Los
recibos modernos enumeran archivos creados, directorios creados y respaldos de
archivos sobrescritos; los recibos antiguos se tratan de forma defensiva.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from styler.execution.processes import ProcessRunner, command_failure_summary, run_step_command
from styler.execution.base import StepExecutor, emit_step_progress
from styler.package_commands import admin_prefix
from styler.planning.models import ExecutionContext, Status, StepDefinition, StepResult


class UndoError(Exception):
    pass


def _home(ctx: ExecutionContext) -> Path:
    injected = ctx.values.get("home")
    return Path(injected).resolve() if injected else Path.home().resolve()


def _inside_home(candidate: Path, home: Path) -> bool:
    """Valida la entrada, no el destino de su último symlink.

    Deshacer puede reemplazar o eliminar un enlace simbólico situado dentro del
    HOME sin seguirlo. Los directorios padre sí se resuelven para impedir que
    un symlink intermedio desvíe la operación fuera del árbol administrado.
    """
    try:
        resolved_home = home.resolve()
        absolute = candidate if candidate.is_absolute() else candidate.absolute()
        resolved_parent = absolute.parent.resolve(strict=False)
    except OSError:
        return False
    if absolute == resolved_home:
        return False
    return resolved_parent == resolved_home or resolved_home in resolved_parent.parents


def _remove_file(path: Path) -> bool:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    return False


def _remove_any_for_full_backup(path: Path) -> bool:
    """Solo se usa cuando existe un respaldo completo para restaurar después."""
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    if path.is_dir():
        shutil.rmtree(path)
        return True
    return False


def _temporary_sibling(path: Path, label: str) -> Path:
    return path.parent / f".{path.name}.styler-{label}-{uuid.uuid4().hex}"


def _restore_file_atomically(backup: Path, target: Path) -> None:
    """Prepara el contenido y reemplaza el destino con un rename atómico."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = _temporary_sibling(target, "restore")
    try:
        shutil.copy2(backup, staged)
        os.replace(staged, target)
    finally:
        if staged.exists() or staged.is_symlink():
            staged.unlink()


def _restore_directory_with_swap(backup: Path, target: Path) -> None:
    """Restaura un árbol mediante staging y swap, con recuperación local."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = _temporary_sibling(target, "restore")
    previous = _temporary_sibling(target, "previous")
    moved_previous = False
    try:
        shutil.copytree(backup, staged, symlinks=True)
        if target.exists() or target.is_symlink():
            os.replace(target, previous)
            moved_previous = True
        try:
            os.replace(staged, target)
        except OSError:
            if moved_previous and previous.exists():
                os.replace(previous, target)
                moved_previous = False
            raise
        if moved_previous:
            _remove_any_for_full_backup(previous)
            moved_previous = False
    finally:
        if staged.exists() or staged.is_symlink():
            _remove_any_for_full_backup(staged)
        # Si el swap terminó pero la limpieza del árbol anterior falló, se
        # intenta una última vez. Nunca se pisa de nuevo el destino restaurado.
        if moved_previous and previous.exists():
            _remove_any_for_full_backup(previous)


class RestoreBackupExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "undo_restore_backup"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        source_raw = str(step.config.get("source") or "")
        backup_raw = str(step.config.get("backup") or "")
        existed = bool(step.config.get("existed", True))
        home = _home(ctx)

        if not source_raw:
            return StepResult.failed(step, "El recibo no indica qué ruta restaurar.", "UNDO_NO_SOURCE")
        source = Path(source_raw)
        if not _inside_home(source, home):
            return StepResult.failed(
                step, f"La ruta {source} está fuera del HOME; no se toca.", "UNDO_OUTSIDE_HOME"
            )

        if ctx.dry_run:
            return StepResult(
                step.id, step.step_type, True, Status.DRY_RUN,
                f"Se restauraría {source} desde {backup_raw or '(no existía antes)' }.",
                data={"source": str(source), "backup": backup_raw, "fully_reverted": True},
            )

        if not existed or not backup_raw:
            # Los archivos creados se eliminan por sus propios recibos. Aquí solo
            # quitamos el contenedor si quedó vacío; jamás borramos contenido que
            # pudo crear la persona después.
            if not source.exists() and not source.is_symlink():
                return StepResult(
                    step.id, step.step_type, True, Status.ROLLED_BACK,
                    f"{source} no existía antes y ya no está presente.",
                    data={"source": str(source), "removed": False, "fully_reverted": True},
                )
            try:
                if source.is_dir() and not source.is_symlink():
                    source.rmdir()
                    removed = True
                elif source.is_file() or source.is_symlink():
                    source.unlink()
                    removed = True
                else:
                    removed = False
            except OSError:
                return StepResult(
                    step.id, step.step_type, True, Status.OK_WITH_WARNINGS,
                    f"{source} no existía antes, pero contiene elementos no atribuidos a Styler; se conservó.",
                    data={
                        "source": str(source),
                        "removed": False,
                        "fully_reverted": False,
                        "pending_reason": "non_empty_new_root",
                    },
                )
            return StepResult(
                step.id, step.step_type, True, Status.ROLLED_BACK,
                f"No había configuración anterior; se quitó {source}." if removed
                else f"No había configuración anterior y {source} ya no existe.",
                data={"source": str(source), "removed": removed, "fully_reverted": True},
            )

        backup = Path(backup_raw)
        if not backup.exists():
            return StepResult.failed(
                step,
                f"El respaldo {backup} ya no existe; no se restaura nada para no empeorar el estado.",
                "UNDO_BACKUP_MISSING",
                "Busca una copia en .styler/component-backups antes de reintentar.",
            )

        try:
            if backup.is_dir():
                _restore_directory_with_swap(backup, source)
            else:
                if source.is_dir() and not source.is_symlink():
                    return StepResult.failed(
                        step,
                        f"El respaldo es un archivo, pero {source} es un directorio; no se reemplazó.",
                        "UNDO_TYPE_MISMATCH",
                    )
                _restore_file_atomically(backup, source)
        except OSError as exc:
            return StepResult.failed(step, f"No se pudo restaurar {source}: {exc}", "UNDO_RESTORE_FAILED")

        return StepResult(
            step.id, step.step_type, True, Status.ROLLED_BACK,
            f"{source} restaurada desde {backup}.",
            data={"source": str(source), "backup": str(backup), "fully_reverted": True},
        )


class RestoreCheckpointExecutor(StepExecutor):
    """Restaura el checkpoint inicial de una integración."""

    @property
    def step_type(self) -> str:
        return "undo_restore_checkpoint"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        raw_paths = step.config.get("paths") or []
        if not isinstance(raw_paths, list):
            return StepResult.failed(
                step,
                "El checkpoint no contiene una lista válida de rutas.",
                "UNDO_CHECKPOINT_CONFIG_ERROR",
            )
        home = _home(ctx)
        restored: list[str] = []
        removed: list[str] = []
        preserved: list[str] = []
        errors: list[str] = []
        outside: list[str] = []

        if ctx.dry_run:
            return StepResult(
                step.id,
                step.step_type,
                True,
                Status.DRY_RUN,
                f"Se restauraría el checkpoint inicial de {len(raw_paths)} ruta(s).",
                data={"fully_reverted": True, "paths": raw_paths},
            )

        for item in raw_paths:
            if not isinstance(item, dict):
                errors.append(str(item))
                continue
            source = Path(str(item.get("path") or ""))
            backup_raw = str(item.get("backup") or "")
            existed = bool(item.get("existed", True))
            if not source:
                errors.append("ruta vacía")
                continue
            if not _inside_home(source, home):
                outside.append(str(source))
                continue
            try:
                if existed:
                    backup = Path(backup_raw)
                    if not backup.exists():
                        errors.append(f"{source}: respaldo ausente {backup}")
                        continue
                    if backup.is_dir():
                        _restore_directory_with_swap(backup, source)
                    else:
                        if source.is_dir() and not source.is_symlink():
                            errors.append(f"{source}: el respaldo es archivo y el destino directorio")
                            continue
                        _restore_file_atomically(backup, source)
                    restored.append(str(source))
                    continue

                if not source.exists() and not source.is_symlink():
                    removed.append(str(source))
                elif source.is_dir() and not source.is_symlink():
                    try:
                        shutil.rmtree(source)
                        removed.append(str(source))
                    except OSError as exc:
                        preserved.append(str(source))
                        errors.append(f"{source}: {exc}")
                elif source.is_file() or source.is_symlink():
                    source.unlink()
                    removed.append(str(source))
                else:
                    preserved.append(str(source))
            except OSError as exc:
                errors.append(f"{source}: {exc}")

        fully_reverted = not (errors or outside or preserved)
        status = Status.ROLLED_BACK if fully_reverted else Status.OK_WITH_WARNINGS
        message = (
            f"Checkpoint restaurado: {len(restored)} ruta(s) restaurada(s), "
            f"{len(removed)} ruta(s) eliminada(s)."
        )
        if preserved:
            message += f" {len(preserved)} ruta(s) se conservaron."
        if outside:
            message += f" {len(outside)} ruta(s) fuera de HOME omitida(s)."
        if errors:
            message += f" {len(errors)} error(es)."
        return StepResult(
            step.id,
            step.step_type,
            not errors,
            status,
            message,
            output="\n".join(restored + removed),
            data={
                "restored": restored,
                "removed": removed,
                "preserved": preserved,
                "errors": errors,
                "skipped_outside_home": outside,
                "fully_reverted": fully_reverted,
            },
        )


class RemovePathsExecutor(StepExecutor):
    """Revierte un conjunto exacto de efectos de escritura."""

    @property
    def step_type(self) -> str:
        return "undo_remove_paths"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        home = _home(ctx)
        raw_created = step.config.get("created_paths") or []
        raw_dirs = step.config.get("created_directories") or []
        overwritten = step.config.get("overwritten") or []

        if isinstance(raw_created, str):
            raw_created = [raw_created]
        if isinstance(raw_dirs, str):
            raw_dirs = [raw_dirs]
        if not isinstance(overwritten, list):
            overwritten = []

        created = [Path(str(item)) for item in raw_created]
        created_dirs = [Path(str(item)) for item in raw_dirs]
        outside: list[str] = []
        inside_created: list[Path] = []
        inside_dirs: list[Path] = []
        for path in created:
            if _inside_home(path, home):
                inside_created.append(path)
            else:
                outside.append(str(path))
        for path in created_dirs:
            if _inside_home(path, home):
                inside_dirs.append(path)
            else:
                outside.append(str(path))

        if ctx.dry_run:
            return StepResult(
                step.id, step.step_type, True, Status.DRY_RUN,
                f"Se revertirían {len(inside_created)} archivo(s), {len(overwritten)} sobrescrito(s) "
                f"y {len(inside_dirs)} directorio(s) creados.",
                data={"fully_reverted": True, "skipped_outside_home": [str(x) for x in outside]},
            )

        removed: list[str] = []
        missing: list[str] = []
        restored: list[str] = []
        errors: list[str] = []
        preserved_directories: list[str] = []
        unrestored: list[str] = []

        # Primero se quitan archivos creados, nunca directorios preexistentes.
        for path in sorted(inside_created, key=lambda item: len(item.parts), reverse=True):
            try:
                if _remove_file(path):
                    removed.append(str(path))
                elif path.is_dir():
                    preserved_directories.append(str(path))
                else:
                    missing.append(str(path))
            except OSError as exc:
                errors.append(f"{path}: {exc}")

        # Después se restauran archivos sobrescritos desde respaldos exactos.
        for item in overwritten:
            if not isinstance(item, dict):
                unrestored.append(str(item))
                continue
            target = Path(str(item.get("path") or ""))
            backup = Path(str(item.get("backup") or ""))
            if not _inside_home(target, home):
                outside.append(str(target))
                continue
            if not backup.is_file():
                unrestored.append(str(target))
                continue
            try:
                if target.is_dir() and not target.is_symlink():
                    unrestored.append(str(target))
                    continue
                _restore_file_atomically(backup, target)
                restored.append(str(target))
            except OSError as exc:
                errors.append(f"{target}: {exc}")

        # Por último, solo rmdir: si la persona dejó algo dentro, se conserva.
        for path in sorted(inside_dirs, key=lambda item: len(item.parts), reverse=True):
            try:
                if not path.exists():
                    missing.append(str(path))
                elif path.is_dir() and not path.is_symlink():
                    path.rmdir()
                    removed.append(str(path))
                else:
                    preserved_directories.append(str(path))
            except OSError:
                preserved_directories.append(str(path))

        fully_reverted = not (errors or outside or preserved_directories or unrestored)
        status = Status.ROLLED_BACK if fully_reverted else Status.OK_WITH_WARNINGS
        message = (
            f"Se quitaron {len(removed)} elemento(s) creados y se restauraron "
            f"{len(restored)} archivo(s) sobrescritos."
        )
        if preserved_directories:
            message += f" Se conservaron {len(preserved_directories)} directorio(s) con contenido ajeno."
        if unrestored:
            message += f" {len(unrestored)} sobrescritura(s) no tienen respaldo utilizable."
        if outside:
            message += f" Se omitieron {len(outside)} rutas fuera del HOME."
        if errors:
            message += f" {len(errors)} operación(es) fallaron."
        return StepResult(
            step.id, step.step_type, not errors, status, message,
            output="\n".join(removed + restored),
            data={
                "removed": removed,
                "restored": restored,
                "missing": missing,
                "errors": errors,
                "skipped_outside_home": [str(item) for item in outside],
                "preserved_directories": preserved_directories,
                "unrestored_overwrites": unrestored,
                "fully_reverted": fully_reverted,
            },
        )


class PackageUninstallExecutor(StepExecutor):
    """Desinstala únicamente un paquete que Styler demostró haber instalado.

    El nodo pertenece al Undo DAG y se construye desde un recibo
    ``PACKAGE_INSTALLED``. Nunca usa ``autoremove``, ``purge`` ni opciones que
    borren datos de usuario. Si otro cambio activo declara necesitar el mismo
    paquete, la operación queda pendiente en vez de adivinar.
    """

    @property
    def step_type(self) -> str:
        return "uninstall_package"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        manager = str(step.config.get("manager") or "")
        package = str(step.config.get("package") or "")
        was_present = bool(step.config.get("was_present", False))
        protected_by = [
            str(item) for item in (step.config.get("protected_by_changes") or []) if item
        ]

        if not manager or not package:
            return StepResult.failed(
                step,
                "El recibo de instalación no identifica gestor y paquete.",
                "UNDO_PACKAGE_CONFIG_ERROR",
            )
        if was_present:
            return StepResult(
                step.id,
                step.step_type,
                True,
                Status.ROLLED_BACK,
                f"{package} ya existía antes del cambio; no se desinstaló.",
                data={"fully_reverted": True, "already_present_before_apply": True},
            )
        if protected_by:
            message = (
                f"{package} también es requerido por: {', '.join(protected_by)}. "
                "Styler conservó el paquete y dejó la decisión pendiente."
            )
            return StepResult(
                step.id,
                step.step_type,
                True,
                Status.WAITING_FOR_USER,
                message,
                data={
                    "fully_reverted": False,
                    "requires_user_action": True,
                    "protected_by_changes": protected_by,
                    "manager": manager,
                    "package": package,
                },
            )

        installed = self._probe_installed(manager, package)
        if installed is None:
            return StepResult.failed(
                step,
                f"Styler no puede comprobar el estado de {package} mediante {manager}.",
                "PACKAGE_STATUS_UNAVAILABLE",
                "No se marcó el recibo como revertido porque la ausencia del paquete no pudo demostrarse.",
            )
        if installed is False:
            return StepResult(
                step.id,
                step.step_type,
                True,
                Status.ROLLED_BACK,
                f"{package} ya no está instalado; el efecto quedó revertido.",
                data={"fully_reverted": True, "already_absent": True},
            )

        argv = self._uninstall_argv(manager, package)
        if argv is None:
            return StepResult.failed(
                step,
                f"El gestor '{manager}' no tiene una desinstalación segura registrada.",
                "PACKAGE_UNINSTALL_UNSUPPORTED",
                "Styler no ejecutó una orden alternativa ni una shell libre.",
            )

        if ctx.dry_run:
            return StepResult(
                step.id,
                step.step_type,
                True,
                Status.DRY_RUN,
                f"Se desinstalaría {package} mediante {manager}.",
                output=json.dumps(argv),
                data={
                    "fully_reverted": True,
                    "manager": manager,
                    "package": package,
                    "argv": argv,
                },
            )

        emit_step_progress(ctx, step, 0.10, f"Preparando la desinstalación de {package}…")
        command = run_step_command(
            ctx, step, argv, timeout=step.timeout,
            label=f"Desinstalando {package} mediante {manager}",
        )
        code, stdout, stderr = command.returncode, command.stdout, command.stderr
        artifact = command.log_path
        if command.timed_out:
            return StepResult(
                step.id,
                step.step_type,
                False,
                Status.TIMEOUT,
                f"La desinstalación de {package} excedió el tiempo permitido.",
                output=stderr or stdout,
                data={"fully_reverted": False, "artifact": artifact, "returncode": code},
            )
        if code != 0:
            reason = command_failure_summary(command)
            message = f"No se pudo desinstalar {package} mediante {manager} (código {code})."
            if reason:
                message += f"\n{reason}"
            result = StepResult.failed(
                step, message, "PACKAGE_UNINSTALL_FAILED",
                f"Consulta {artifact}; Styler no intentó autoremove ni una alternativa destructiva.",
            )
            result.output = command.stdout or command.stderr
            result.data.update({
                "fully_reverted": False, "artifact": artifact, "returncode": code,
                "manager": manager, "package": package,
                "command": " ".join(command.command), "failure_tail": reason,
            })
            return result
        verified = self._probe_installed(manager, package)
        if verified is None:
            return StepResult.failed(
                step,
                f"El gestor terminó, pero Styler no pudo verificar la ausencia de {package}.",
                "PACKAGE_UNINSTALL_VERIFICATION_UNAVAILABLE",
                f"Consulta {artifact}; el recibo sigue pendiente.",
            )
        if verified:
            return StepResult.failed(
                step,
                f"{package} continúa instalado después de que el gestor terminó.",
                "PACKAGE_UNINSTALL_NOT_VERIFIED",
                f"Consulta {artifact}.",
            )

        emit_step_progress(ctx, step, 1.0, f"{package} fue desinstalado.")
        return StepResult(
            step.id,
            step.step_type,
            True,
            Status.ROLLED_BACK,
            f"Paquete desinstalado: {package}.",
            output=stdout,
            data={
                "fully_reverted": True,
                "manager": manager,
                "package": package,
                "artifact": artifact,
                "returncode": code,
            },
        )

    @staticmethod
    def _probe_installed(manager: str, package: str) -> bool | None:
        """Devuelve True/False solo cuando el gestor puede demostrarlo."""
        argv: list[str] | None = None
        if manager == "apt" and shutil.which("dpkg-query"):
            argv = ["dpkg-query", "-W", "-f=${Status}", package]
        elif manager == "flatpak" and shutil.which("flatpak"):
            argv = ["flatpak", "info", package]
        elif manager in {"pacman", "aur"} and shutil.which("pacman"):
            argv = ["pacman", "-Q", package]
        elif manager in {"rpm", "zypper"} and shutil.which("rpm"):
            argv = ["rpm", "-q", package]
        elif manager == "snap" and shutil.which("snap"):
            argv = ["snap", "list", package]
        if argv is None:
            return None
        result = ProcessRunner(timeout=15).run(argv, timeout=15)
        if result.returncode == 127:
            return None
        if manager == "apt":
            return result.returncode == 0 and "install ok installed" in result.stdout
        return result.returncode == 0

    @staticmethod
    def _uninstall_argv(manager: str, package: str) -> list[str] | None:
        prefix = admin_prefix()
        if manager == "apt" and shutil.which("apt-get") and prefix is not None:
            return [*prefix, "apt-get", "remove", "-y", package]
        if manager == "flatpak" and shutil.which("flatpak"):
            return ["flatpak", "uninstall", "-y", package]
        if manager in {"pacman", "aur"} and shutil.which("pacman") and prefix is not None:
            return [*prefix, "pacman", "-R", "--noconfirm", package]
        if manager == "rpm" and shutil.which("dnf") and prefix is not None:
            return [*prefix, "dnf", "remove", "-y", package]
        if manager == "zypper" and shutil.which("zypper") and prefix is not None:
            return [*prefix, "zypper", "--non-interactive", "remove", package]
        if manager == "snap" and shutil.which("snap") and prefix is not None:
            return [*prefix, "snap", "remove", package]
        return None




class RestoreSettingExecutor(StepExecutor):
    """Restaura un ajuste visual estructurado desde el valor registrado."""

    @property
    def step_type(self) -> str:
        return "undo_restore_setting"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        backend = str(step.config.get("backend") or "")
        schema = str(step.config.get("schema") or "")
        group = str(step.config.get("group") or "")
        key = str(step.config.get("key") or "")
        previous = str(step.config.get("previous_value") or "")
        previous_exists = bool(step.config.get("previous_exists", True))
        if backend not in {"gsettings", "kconfig"} or not key:
            return StepResult.failed(step, "El recibo del ajuste es incompleto.", "UNDO_SETTING_CONFIG_ERROR")

        if backend == "gsettings":
            if not schema or not shutil.which("gsettings"):
                return StepResult.failed(step, "GSettings no está disponible para restaurar el ajuste.", "UNDO_SETTING_TOOL_MISSING")
            argv = ["gsettings", "set", schema, key, previous]
        else:
            writer = shutil.which("kwriteconfig6") or shutil.which("kwriteconfig5")
            if not writer or not schema or not group:
                return StepResult.failed(step, "KConfig no está disponible para restaurar el ajuste.", "UNDO_SETTING_TOOL_MISSING")
            argv = [writer, "--file", schema, "--group", group, "--key", key]
            if previous_exists:
                argv.append(previous)
            else:
                argv.append("--delete")

        if ctx.dry_run:
            return StepResult(
                step.id, step.step_type, True, Status.DRY_RUN,
                f"Se restauraría el ajuste {backend}:{schema}:{key}.",
                data={"backend": backend, "schema": schema, "group": group, "key": key},
            )
        result = run_step_command(
            ctx, step, argv, timeout=step.timeout,
            label=f"Restaurando ajuste {key}",
        )
        if result.returncode != 0:
            return StepResult.failed(
                step, f"No se pudo restaurar el ajuste {key}: {result.stderr or result.stdout}",
                "UNDO_SETTING_FAILED",
            )
        return StepResult(
            step.id, step.step_type, True, Status.ROLLED_BACK,
            f"Ajuste restaurado: {backend}:{schema}:{key}.",
            data={"backend": backend, "schema": schema, "group": group, "key": key, "fully_reverted": True},
        )


class UndoNoteExecutor(StepExecutor):
    """Registra un efecto que requiere una decisión humana."""

    @property
    def step_type(self) -> str:
        return "undo_note"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        message = step.description or "Efecto no reversible automáticamente."
        return StepResult(
            step.id, step.step_type, True, Status.WAITING_FOR_USER, message,
            output=message,
            data={**dict(step.config), "fully_reverted": False, "requires_user_action": True},
        )
