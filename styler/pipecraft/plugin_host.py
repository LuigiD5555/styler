"""Host de un nodo Styler bajo el executor `plugin` de PipeCraft.

Un proceso ejecuta exactamente un nodo. El JSON final usa stdout; el progreso
estructurado se envía por stderr y PipeCraft lo conserva como eventos de proceso.
"""
from __future__ import annotations

import contextlib
import json
import signal
import sys
from pathlib import Path
from typing import Any

from styler.component_catalog.executors import extended_registry
from styler.runtime.commands import PipeCraftRunner
from styler.runtime.models import CheckAttachment, ExecutionContext, StepDefinition

PREFIX = "STYLER_EVENT\t"

PIPECRAFT_STATUSES = {
    "ok", "ok_with_warnings", "skipped", "failed", "needs_approval",
    "dry_run", "planned", "timeout", "cancelled", "pending", "ready",
    "running", "succeeded", "blocked",
}


def _runtime_status(success: bool, styler_status: str) -> str:
    """Normaliza sólo la vista del runtime; el estado Styler viaja en data."""
    if styler_status in PIPECRAFT_STATUSES:
        return styler_status
    return "ok" if success else "failed"


def _step(raw: dict[str, Any]) -> StepDefinition:
    value = dict(raw)
    checks = value.get("checks")
    if isinstance(checks, dict):
        # Los checks generados ya forman nodos separados en el plan compilado;
        # aquí sólo conservamos un contenedor válido para compatibilidad.
        value["checks"] = CheckAttachment()
    known = StepDefinition.__dataclass_fields__
    return StepDefinition(**{key: val for key, val in value.items() if key in known})


def _emit(event: dict[str, Any]) -> None:
    sys.stderr.write(PREFIX + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stderr.flush()


def main() -> int:
    out = sys.stdout
    try:
        payload = json.load(sys.stdin)
        if payload.get("protocol") != "pipecraft.plugin/v1":
            raise ValueError("protocolo de plugin incompatible")
        values = dict(payload.get("with") or {})
        step = _step(dict(values.get("styler_step") or {}))
        node = dict(values.get("styler_node") or {})
        ctx_values = dict(values.get("styler_context") or {})
        styler_root = Path(str(ctx_values.pop("styler_root", payload.get("root") or ".")))
        runner = PipeCraftRunner()
        ctx_values["command_runner"] = runner
        ctx_values["progress_callback"] = _emit
        run_dir = Path(str(payload.get("logs_dir") or ".")).parent
        ctx = ExecutionContext(
            root=styler_root,
            dry_run=bool(payload.get("dry_run", False)),
            approve=True,
            labels=[str(v) for v in payload.get("labels", [])],
            values=ctx_values,
            run_id=str(payload.get("run_id", "")),
            run_dir=run_dir,
            artifacts_dir=Path(str(payload.get("artifacts_dir") or run_dir / "artifacts")),
            logs_dir=Path(str(payload.get("logs_dir") or run_dir / "logs")),
            events_path=run_dir / "events.jsonl",
            plan_path=run_dir / "plan.json",
        )

        def stop(_sig, _frame) -> None:
            runner.request_cancel()

        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, stop)
        if hasattr(signal, "SIGINT"):
            signal.signal(signal.SIGINT, stop)

        executor = extended_registry().get(step.step_type)
        if executor is None:
            raise ValueError(f"Styler no tiene executor para {step.step_type!r}")

        result = None
        if bool(ctx.values.get("continuation_mode", False)):
            try:
                # Reconcile también debe respetar stdout reservado al protocolo.
                with contextlib.redirect_stdout(sys.stderr):
                    result = executor.reconcile(step, ctx)
            except Exception as exc:  # la ejecución normal sigue siendo fuente final
                _emit({"step_id": step.id, "event_type": "reconciliation_warning", "message": str(exc)})
                result = None
        if result is None:
            # Nada salvo el JSON final puede contaminar stdout del protocolo.
            with contextlib.redirect_stdout(sys.stderr):
                result = executor.run(step, ctx)
        result.step_id = str(node.get("id") or result.step_id)
        result.node_id = result.step_id
        result.source_step_id = str(node.get("source_id") or step.id)
        result.node_kind = str(node.get("kind") or result.node_kind)
        result.phase = str(node.get("phase") or "")
        result.block = str(node.get("block") or "")
        if bool(ctx.values.get("continuation_mode", False)) and result.success and result.data.get("reconciled"):
            result.status = "reconciled"

        result.data.setdefault("styler_step_type", step.step_type)
        result.data.setdefault("styler_source_step_id", result.source_step_id)
        result.data.setdefault("styler_node_kind", result.node_kind)
        result.data.setdefault("styler_phase", result.phase)
        result.data.setdefault("styler_block", result.block)
        # PipeCraft persiste sólo estados canónicos para decidir dependencias y
        # resume. Styler posee estados semánticos adicionales (reconciled,
        # rolled_back, requires_reboot, ...). Conservamos el estado de dominio
        # en `data` y exponemos al runtime un estado que entienda sin ambigüedad.
        styler_status = str(result.status)
        result.data.setdefault("styler_status", styler_status)
        runtime_status = _runtime_status(bool(result.success), styler_status)
        response = {
            "success": bool(result.success),
            "status": runtime_status,
            "message": str(result.message),
            "output": str(result.output),
            "data": dict(result.data),
        }
        out.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        out.flush()
        return 0 if result.success else 1
    except BaseException as exc:  # el protocolo siempre intenta devolver diagnóstico estructurado
        response = {
            "success": False,
            "status": "failed",
            "message": f"Styler plugin host: {exc}",
            "data": {"error_type": type(exc).__name__},
        }
        try:
            out.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            out.flush()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
