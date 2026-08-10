"""Registro y ejecutores de operaciones concretas de Styler."""

from __future__ import annotations

import json
import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from styler.applications import apt_install_argv
from styler.runtime.commands import PipeCraftRunner, command_failure_summary, run_step_command
from styler.runtime.models import ExecutionContext, Status, StepDefinition, StepResult


class StepExecutor(ABC):
    @property
    @abstractmethod
    def step_type(self) -> str:
        raise NotImplementedError

    def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        """Reconoce un efecto ya satisfecho sin volver a producirlo.

        Debe ser una consulta sin efectos laterales. Los ejecutores concretos
        pueden devolver un resultado ``RECONCILED`` cuando el estado real del
        equipo demuestra que el paso ya se completó. ``None`` significa que el
        paso continúa pendiente y debe ejecutarse normalmente.
        """
        return None

    @abstractmethod
    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        raise NotImplementedError


class ExecutorRegistry:
    def __init__(self) -> None:
        self._executors: dict[str, StepExecutor] = {}

    def register(self, executor: StepExecutor) -> None:
        self._executors[executor.step_type] = executor

    def get(self, step_type: str) -> StepExecutor | None:
        return self._executors.get(step_type)

    def known_types(self) -> set[str]:
        return set(self._executors)

    @classmethod
    def default(cls) -> "ExecutorRegistry":
        registry = cls()
        registry.register(NoteExecutor())
        registry.register(PackageInstallExecutor())
        registry.register(FileOverlayExecutor())
        registry.register(ServiceEnableExecutor())
        # Import local para evitar que el runtime base dependa circularmente del
        # subsistema opcional de automatización al cargar el módulo.
        from styler.automation.executors import (
            DesktopClickStepExecutor,
            LaunchApplicationStepExecutor,
            SleepStepExecutor,
            WaitUntilStepExecutor,
        )
        registry.register(SleepStepExecutor())
        registry.register(WaitUntilStepExecutor())
        registry.register(DesktopClickStepExecutor())
        registry.register(LaunchApplicationStepExecutor())
        # Reversión compilada desde los recibos (styler/receipts.py).
        from styler.runtime.undo_executors import (
            PackageUninstallExecutor,
            RemovePathsExecutor,
            RestoreCheckpointExecutor,
            RestoreBackupExecutor,
            RestoreSettingExecutor,
            UndoNoteExecutor,
        )
        registry.register(RestoreBackupExecutor())
        registry.register(RestoreCheckpointExecutor())
        registry.register(RemovePathsExecutor())
        registry.register(PackageUninstallExecutor())
        registry.register(RestoreSettingExecutor())
        registry.register(UndoNoteExecutor())
        return registry


def _write_artifact(ctx: ExecutionContext, step_id: str, name: str, content: str) -> str:
    folder = ctx.artifacts_dir / step_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def emit_step_progress(
    ctx: ExecutionContext,
    step: StepDefinition,
    progress: float | None,
    operation: str,
    *,
    message: str = "",
) -> None:
    """Publica progreso sin acoplar los ejecutores a una interfaz concreta."""
    callback = ctx.values.get("progress_callback")
    if not callable(callback):
        return
    try:
        callback({
            "step_id": step.id,
            "phase_progress": progress,
            "status": "running",
            "operation": operation,
            "message": message or operation,
        })
    except Exception:
        pass



class NoteExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "note"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        message = str(step.config.get("message") or step.description or step.id)
        return StepResult(step.id, step.step_type, True, Status.OK, message, output=message)


class PackageInstallExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "install_package"

    def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        package = dict(step.config.get("package") or {})
        manager = str(package.get("manager", ""))
        name = str(package.get("name", ""))
        if not manager or not name or not self._is_installed(manager, name):
            return None

        receipt_id = ""
        change_id = str(ctx.values.get("change_id") or "")
        if change_id:
            try:
                from styler.receipts import ReceiptJournal, ReceiptKind

                journal = ReceiptJournal(ctx.values.get("receipts_root") or ctx.root, change_id)
                receipt = next(
                    (
                        item
                        for item in reversed(journal.pending_undo())
                        if item.kind == ReceiptKind.PACKAGE_INSTALLED
                        and str(item.data.get("manager") or "") == manager
                        and str(item.data.get("package") or "") == name
                    ),
                    None,
                )
                receipt_id = receipt.receipt_id if receipt is not None else ""
            except (OSError, ValueError):
                receipt_id = ""

        message = (
            f"Styler ya había instalado {name}; se reutilizará la instalación registrada."
            if receipt_id
            else f"{name} ya está instalado en el equipo; no se volverá a instalar."
        )
        package_facts: dict[str, object] = {}
        if manager == "flatpak":
            try:
                from styler.flatpak_facts import inspect_flatpak_application, save_flatpak_facts
                observed = inspect_flatpak_application(name)
                package_facts = observed.to_dict()
                if observed.installed:
                    save_flatpak_facts(ctx.root, observed)
            except Exception:  # noqa: BLE001 - la reconciliación sigue siendo conservadora
                package_facts = {}
        return StepResult(
            step.id,
            step.step_type,
            True,
            Status.RECONCILED,
            message,
            data={
                "manager": manager,
                "name": name,
                "already_present": True,
                "reconciled": True,
                "evidence": "receipt+system" if receipt_id else "system",
                "receipt_id": receipt_id,
                "package_facts": package_facts,
            },
        )

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        package = dict(step.config.get("package") or {})
        manager = str(package.get("manager", ""))
        name = str(package.get("name", ""))
        if not manager or not name:
            return StepResult.failed(step, "Falta manager o name del paquete.", "PACKAGE_CONFIG_ERROR")

        emit_step_progress(ctx, step, 0.03, f"Comprobando si {name} ya está instalado…")

        if ctx.dry_run:
            return StepResult(
                step.id,
                step.step_type,
                True,
                Status.DRY_RUN,
                f"Se instalaría {name} mediante {manager}.",
                data={"manager": manager, "name": name},
            )

        from styler.receipts import ReceiptWriteError, ensure_receipts_writable
        try:
            ensure_receipts_writable(ctx)
        except ReceiptWriteError as exc:
            return StepResult.failed(
                step, str(exc), "RECEIPT_JOURNAL_UNAVAILABLE",
                "La instalación no comenzó porque Styler no podía registrar sus efectos.",
            )

        if manager == "flatpak":
            flathub = self._ensure_flathub(ctx, step)
            if flathub is not None:
                return flathub
        emit_step_progress(ctx, step, 0.10, f"Preparando la instalación mediante {manager}…")
        argv = self._install_argv(manager, name)
        if argv is None:
            return StepResult.failed(
                step,
                f"El gestor '{manager}' todavía no tiene instalador integrado.",
                "PACKAGE_MANAGER_UNSUPPORTED",
            )

        emit_step_progress(
            ctx, step, None,
            f"El gestor {manager} está procesando {name}…",
            message="El porcentaje interno depende del gestor; el progreso total sigue visible.",
        )
        command = run_step_command(
            ctx, step, argv, timeout=step.timeout,
            label=f"Instalando {name} mediante {manager}",
        )
        code, stdout, stderr = command.returncode, command.stdout, command.stderr
        artifact = command.log_path
        if command.timed_out:
            return StepResult(
                step.id,
                step.step_type,
                False,
                Status.TIMEOUT,
                f"La instalación de {name} excedió el tiempo permitido.",
                output=stderr or stdout,
                data={"artifact": artifact, "returncode": code},
            )
        if code != 0:
            if self._is_installed(manager, name):
                from styler.receipts import ReceiptKind, emit_receipt

                partial_data: dict[str, object] = {
                    "manager": manager, "package": name, "was_present": False, "partial": True
                }
                if manager == "flatpak":
                    try:
                        from styler.flatpak_facts import inspect_flatpak_application, save_flatpak_facts
                        observed = inspect_flatpak_application(name)
                        partial_data["facts"] = observed.to_dict()
                        if observed.installed:
                            save_flatpak_facts(ctx.root, observed)
                    except Exception:  # noqa: BLE001
                        pass
                emit_receipt(ctx, step, ReceiptKind.PACKAGE_INSTALLED, partial_data)
            reason = command_failure_summary(command)
            message = f"No se pudo instalar {name} mediante {manager} (código {code})."
            if reason:
                message += f"\n{reason}"
            result = StepResult.failed(
                step,
                message,
                "PACKAGE_INSTALL_FAILED",
                "Styler detuvo el DAG. Revisa la causa y el log antes de reintentar; si el gestor dejó efectos parciales, quedaron registrados para Deshacer.",
            )
            result.output = command.stdout or command.stderr
            result.data.update({
                "artifact": artifact,
                "returncode": code,
                "manager": manager,
                "package": name,
                "command": " ".join(command.command),
                "failure_tail": reason,
            })
            return result
        # El recibo deja constancia de que el paquete NO estaba antes. Styler no
        # lo desinstala al deshacer, pero sí puede decirlo con certeza.
        from styler.receipts import ReceiptKind, emit_receipt

        receipt_data: dict[str, object] = {
            "manager": manager, "package": name, "was_present": False
        }
        installed_facts: dict[str, object] = {}
        if manager == "flatpak":
            try:
                from styler.flatpak_facts import inspect_flatpak_application, save_flatpak_facts
                observed = inspect_flatpak_application(name)
                installed_facts = observed.to_dict()
                receipt_data["facts"] = installed_facts
                if observed.installed:
                    save_flatpak_facts(ctx.root, observed)
            except Exception:  # noqa: BLE001
                installed_facts = {}
        emit_receipt(ctx, step, ReceiptKind.PACKAGE_INSTALLED, receipt_data)
        return StepResult(
            step.id,
            step.step_type,
            True,
            Status.OK,
            f"Paquete instalado: {name}.",
            output=stdout,
            data={"artifact": artifact, "returncode": code, "package_facts": installed_facts},
        )

    @staticmethod
    def _is_installed(manager: str, name: str) -> bool:
        commands: list[str] | None = None
        if manager == "apt" and shutil.which("dpkg-query"):
            commands = ["dpkg-query", "-W", "-f=${Status}", name]
        elif manager == "flatpak" and shutil.which("flatpak"):
            commands = ["flatpak", "info", name]
        elif manager in {"pacman", "aur"} and shutil.which("pacman"):
            commands = ["pacman", "-Q", name]
        elif manager == "rpm" and shutil.which("rpm"):
            commands = ["rpm", "-q", name]
        elif manager == "zypper" and shutil.which("rpm"):
            commands = ["rpm", "-q", name]
        elif manager == "snap" and shutil.which("snap"):
            commands = ["snap", "list", name]
        if commands is None:
            return False
        result = PipeCraftRunner(timeout=20).run(commands, timeout=20)
        if manager == "apt":
            return result.returncode == 0 and "install ok installed" in result.stdout
        return result.returncode == 0

    @staticmethod
    def _privileged_prefix() -> list[str] | None:
        if os.geteuid() == 0:
            return []
        if shutil.which("sudo"):
            return ["sudo", "-n"]
        if shutil.which("pkexec"):
            return ["pkexec"]
        return None

    @classmethod
    def _install_argv(cls, manager: str, name: str) -> list[str] | None:
        prefix = cls._privileged_prefix()
        if manager == "apt" and shutil.which("apt-get") and prefix is not None:
            return apt_install_argv(prefix, name)
        if manager == "flatpak" and shutil.which("flatpak"):
            return ["flatpak", "install", "-y", "flathub", name]
        if manager == "pacman" and shutil.which("pacman") and prefix is not None:
            return [*prefix, "pacman", "-S", "--needed", "--noconfirm", name]
        if manager == "aur":
            helper = shutil.which("yay") or shutil.which("paru")
            return [helper, "-S", "--needed", "--noconfirm", name] if helper else None
        if manager == "rpm" and shutil.which("dnf") and prefix is not None:
            return [*prefix, "dnf", "install", "-y", name]
        if manager == "zypper" and shutil.which("zypper") and prefix is not None:
            return [*prefix, "zypper", "--non-interactive", "install", name]
        if manager == "snap" and shutil.which("snap") and prefix is not None:
            return [*prefix, "snap", "install", name]
        return None

    @staticmethod
    def _ensure_flathub(
        ctx: ExecutionContext, step: StepDefinition
    ) -> StepResult | None:
        if not shutil.which("flatpak"):
            return StepResult.failed(step, "Flatpak no está disponible.", "FLATPAK_NOT_FOUND")
        probe = run_step_command(
            ctx, step, ["flatpak", "remotes", "--columns=name"], timeout=20,
            label="Comprobando el remoto Flathub",
        )
        if probe.returncode != 0:
            return StepResult.failed(
                step, "No se pudieron consultar los remotos de Flatpak.",
                "FLATPAK_REMOTES_FAILED", f"Consulta {probe.log_path}.",
            )
        names = {line.strip().lower() for line in probe.stdout.splitlines()}
        if "flathub" in names:
            return None
        added = run_step_command(
            ctx, step,
            [
                "flatpak", "remote-add", "--if-not-exists", "flathub",
                "https://flathub.org/repo/flathub.flatpakrepo",
            ],
            timeout=60,
            label="Añadiendo el remoto Flathub",
        )
        if added.returncode != 0:
            return StepResult.failed(
                step, "No se pudo configurar el remoto Flathub.",
                "FLATHUB_SETUP_FAILED", f"Consulta {added.log_path}.",
            )
        return None



class FileOverlayExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "apply_file_overlay"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        from styler.objectstore import ObjectStore, ObjectStoreError
        from styler.validation import ValidationError, resolve_home_path, validate_checksum, validate_logical_path

        files = list(step.config.get("files") or [])
        planned = [str(item.get("path", "")) for item in files if item.get("path")]
        artifact = _write_artifact(ctx, step.id, "planned-files.txt", "\n".join(planned))
        if ctx.dry_run:
            return StepResult(
                step.id, step.step_type, True, Status.DRY_RUN,
                f"Se aplicarían {len(planned)} archivos.",
                output="\n".join(planned),
                data={"planned_files": len(planned), "artifact": artifact},
            )

        store = ObjectStore(root=ctx.root)
        validated: list[tuple[dict, Path]] = []
        try:
            for item in files:
                logical = validate_logical_path(str(item.get("path", "")))
                checksum = validate_checksum(str(item.get("checksum", "")))
                destination = resolve_home_path(logical, Path.home())
                if destination.exists() and (destination.is_symlink() or destination.is_dir()):
                    raise ValidationError(f"Destino no escribible como archivo: {logical}")
                if not store.verify(checksum):
                    raise ObjectStoreError(f"Objeto ausente o corrupto: {checksum}")
                validated.append((item, destination))
        except (ValidationError, ObjectStoreError) as exc:
            return StepResult.failed(step, str(exc), "FILE_PREFLIGHT_FAILED")

        applied: list[str] = []
        diagnostics: list[dict] = []
        try:
            for item, destination in validated:
                mode_value = item.get("mode")
                mode = None
                if mode_value:
                    mode = int(str(mode_value), 8)
                store.restore_file(str(item["checksum"]), destination, mode=mode)
                from styler.launcher_integrity import normalize_and_inspect
                inspection = normalize_and_inspect(destination, Path.home(), mode)
                applied.append(str(destination))
                if inspection.missing_commands or inspection.missing_paths or inspection.notes:
                    diagnostics.append({
                        "path": str(destination),
                        "changed": inspection.changed,
                        "complete": inspection.complete,
                        "missing_commands": inspection.missing_commands,
                        "missing_paths": inspection.missing_paths,
                        "notes": inspection.notes,
                    })
        except (OSError, ValueError, ObjectStoreError) as exc:
            return StepResult.failed(step, f"No se pudo aplicar la configuración: {exc}", "FILE_APPLY_FAILED")

        applied_artifact = _write_artifact(ctx, step.id, "applied-files.txt", "\n".join(applied))
        return StepResult(
            step.id, step.step_type, True, Status.OK,
            f"Se aplicaron {len(applied)} archivos.",
            output="\n".join(applied),
            data={
                "applied_files": len(applied),
                "artifact": applied_artifact,
                "launcher_diagnostics": diagnostics,
                "incomplete_launchers": sum(1 for item in diagnostics if not item.get("complete", True)),
            },
        )


class ServiceEnableExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "enable_service"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        service = dict(step.config.get("service") or {})
        name = str(service.get("name", ""))
        scope = str(service.get("scope", "user"))
        if not name:
            return StepResult.failed(step, "Falta el nombre del servicio.", "SERVICE_CONFIG_ERROR")
        if ctx.dry_run:
            return StepResult(
                step.id,
                step.step_type,
                True,
                Status.DRY_RUN,
                f"Se habilitaría el servicio {name} ({scope}).",
            )
        if not shutil.which("systemctl"):
            return StepResult.failed(step, "systemctl no está disponible.", "SYSTEMCTL_NOT_FOUND")

        if scope == "user":
            argv = ["systemctl", "--user", "enable", "--now", name]
        elif os.geteuid() == 0:
            argv = ["systemctl", "enable", "--now", name]
        elif shutil.which("sudo"):
            argv = ["sudo", "-n", "systemctl", "enable", "--now", name]
        else:
            return StepResult.failed(
                step,
                "El servicio de sistema requiere permisos y sudo no está disponible.",
                "SERVICE_PERMISSION_REQUIRED",
            )

        command = run_step_command(
            ctx, step, argv, timeout=step.timeout,
            label=f"Habilitando el servicio {name}",
        )
        code, stdout, stderr = command.returncode, command.stdout, command.stderr
        artifact = command.log_path
        if command.timed_out:
            return StepResult(
                step.id,
                step.step_type,
                False,
                Status.TIMEOUT,
                f"La operación del servicio {name} excedió el tiempo permitido.",
                data={"artifact": artifact},
            )
        if code != 0:
            reason = command_failure_summary(command)
            message = f"No se pudo habilitar {name} (código {code})."
            if reason:
                message += f"\n{reason}"
            result = StepResult.failed(step, message, "SERVICE_ENABLE_FAILED")
            result.output = command.stdout or command.stderr
            result.data.update({
                "artifact": artifact,
                "returncode": code,
                "command": " ".join(command.command),
                "failure_tail": reason,
            })
            return result
        return StepResult(
            step.id,
            step.step_type,
            True,
            Status.OK,
            f"Servicio habilitado: {name}.",
            output=stdout,
            data={"artifact": artifact},
        )


def _expand_portable_path(path: str) -> Path:
    if path == "${HOME}":
        return Path.home()
    if path.startswith("${HOME}/"):
        return Path.home() / path[len("${HOME}/") :]
    return Path(path)
