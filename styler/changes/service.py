"""API pública del subsistema de cambios de Styler.

La lógica está separada por algoritmos reales; ``ChangeService`` conserva la
misma superficie pública para TUI/CLI y compone esas capacidades sin wrappers.
"""
from __future__ import annotations

from ._service_support import *  # noqa: F401,F403
from ._service_support import _CONFIG_VERSION_DIR, _change_name
from .discovery import DiscoveryOperations
from .planner import PlanningOperations
from .execution import ExecutionOperations
from .removal import RemovalOperations


class ChangeService(
    DiscoveryOperations,
    PlanningOperations,
    ExecutionOperations,
    RemovalOperations,
):
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

    def _assert_execution_storage_writable(self, change_id: str) -> None:
        """Comprueba los almacenes durables antes de iniciar efectos."""
        for directory in (
            self._state_dir,
            self.root / ".styler" / "runs",
            self.root / ".styler" / "receipts",
            self.root / ".styler" / "pipecraft" / ".pipelines",
        ):
            probe_directory_writable(directory)
        try:
            self.journal_for_change(change_id).ensure_writable()
        except OSError as exc:
            raise storage_error(
                exc, self.root / ".styler" / "receipts" / f"{change_id}.jsonl"
            ) from exc

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
        mount_line = mount_status(exc.path.parent)
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
                "mount": mount_status(exc.path.parent),
                "run_report": run_report,
                "created_at": time.time(),
            }
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            return str(path)
        except OSError:
            return ""
