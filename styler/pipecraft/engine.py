"""Backend PipeCraft 1.5 para WorkflowDefinition de Styler."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from styler.runtime.graph import topological_order
from styler.runtime.models import (
    ExecutionContext,
    ExecutionPlan,
    NodeKind,
    PipelineSummary,
    Status,
    StepResult,
    WorkflowDefinition,
    WorkflowRun,
)
from styler.runtime.plan import compile_workflow, plan_to_dict
from styler.runtime.selection import select_plan_nodes

from .client import PipeCraftIpcError
from .compiler import compile_pipeline
from .service import PipeCraftUnavailable, ensure_service, prepare_workspace

PREFIX = "STYLER_EVENT\t"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summary(results: list[StepResult]) -> PipelineSummary:
    summary = PipelineSummary()
    for result in results:
        if result.node_kind == NodeKind.CHECK:
            summary.checks += 1
        elif result.node_kind == NodeKind.HOOK:
            summary.hooks += 1
        else:
            summary.actions += 1
        if result.status == Status.SKIPPED:
            summary.skipped += 1
        elif result.status == Status.BLOCKED:
            summary.blocked += 1
        elif result.status == Status.OK_WITH_WARNINGS:
            summary.succeeded += 1; summary.warnings += 1
        elif result.success:
            summary.succeeded += 1
        else:
            summary.failed += 1
    return summary


class PipeCraftBackend:
    """Traduce Styler -> PipeCraft; PipeCraft es dueño de la ejecución."""

    def __init__(self, styler_root: Path) -> None:
        self.styler_root = Path(styler_root)

    @staticmethod
    def available() -> bool:
        if os.environ.get("STYLER_RUNTIME", "auto").lower() == "local":
            return False
        from .service import locate_binary
        return locate_binary() is not None

    def run(self, workflow: WorkflowDefinition, ctx: ExecutionContext, plan: ExecutionPlan | None = None) -> WorkflowRun:
        started = _now()
        plan = plan or compile_workflow(workflow)
        selected = select_plan_nodes(plan, ctx)
        workspace = prepare_workspace(self.styler_root)
        pipeline_dir = workspace / ".pipelines" / "pipelines"
        # El nombre del archivo y el name YAML deben coincidir para catalog.load().
        provisional = pipeline_dir / "styler-transient.yaml"
        pipeline_name = compile_pipeline(workflow, plan, ctx, selected, provisional)
        pipeline_path = pipeline_dir / f"{pipeline_name}.yaml"
        if pipeline_path != provisional:
            provisional.replace(pipeline_path)

        client = ensure_service(self.styler_root)
        max_workers = int(workflow.metadata.get("max_workers", 4) or 4)
        run_id = client.submit(
            pipeline_name,
            execute=not ctx.dry_run,
            approve=ctx.approve,
            labels=list(ctx.labels),
            max_workers=max_workers,
        )
        submitted = ctx.values.get("run_submitted_callback")
        if callable(submitted):
            try:
                submitted(run_id)
            except Exception:
                # Si Styler no puede persistir el identificador durable, no
                # dejamos un job huérfano ejecutando efectos sin referencia.
                try:
                    client.cancel(run_id)
                finally:
                    raise
        callback = ctx.values.get("progress_callback")
        total = max(1, len(selected))
        completed: set[str] = set()

        def progress_event(event: dict[str, Any]) -> None:
            step_id = str(event.get("step_id") or "")
            kind = str(event.get("event") or "")
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if kind == "process_output" and str(data.get("stream")) == "stderr":
                text = str(data.get("text") or "")
                for line in text.splitlines():
                    if line.startswith(PREFIX):
                        try:
                            raw = json.loads(line[len(PREFIX):])
                        except json.JSONDecodeError:
                            continue
                        if callable(callback):
                            raw.setdefault("total_progress", len(completed) / total)
                            callback(raw)
                return
            if kind == "node_finished":
                completed.add(step_id)
            if callable(callback) and kind in {"node_started", "node_finished", "node_blocked", "node_cancelled", "node_skipped", "process_heartbeat", "process_timeout"}:
                status = "running"
                if kind == "node_finished": status = "completed"
                elif kind in {"node_blocked", "node_cancelled"}: status = "failed"
                elif kind == "node_skipped": status = "skipped"
                payload = {
                    "step_id": step_id,
                    "event_type": kind,
                    "status": status,
                    "phase_progress": 1.0 if kind == "node_finished" else None,
                    "total_progress": min(1.0, len(completed) / total),
                    "operation": str(data.get("message") or kind.replace("_", " ")),
                    "message": str(data.get("message") or ""),
                    "elapsed_seconds": float(data.get("elapsed_ms", 0) or 0) / 1000.0,
                    "quiet_seconds": float(data.get("inactive_ms", 0) or 0) / 1000.0,
                }
                callback(payload)

        job = client.wait(run_id, progress=progress_event)
        if job.status == "interrupted":
            raise PipeCraftIpcError(
                f"PipeCraft interrumpió {run_id}. Styler debe reconciliar el estado real antes de reanudar. {job.warning}"
            )
        report = client.report(run_id)
        results = self._results(report, plan)
        success = bool(report.get("success", False)) and job.status == "succeeded"
        run_dir = str(report.get("run_dir") or "")
        run = WorkflowRun(
            run_id=run_id,
            workflow=workflow.name,
            success=success,
            dry_run=ctx.dry_run,
            started_at=str(report.get("started_at") or started),
            finished_at=str(report.get("finished_at") or _now()),
            results=results,
            operation=workflow.operation,
            status=job.status,
            order=[str(v) for v in report.get("order", [])],
            pipeline_fingerprint=plan.pipeline_fingerprint,
            plan_fingerprint=str(report.get("plan_fingerprint") or plan.plan_fingerprint),
            summary=_summary(results),
            selected_from=ctx.from_step or "",
            selected_downstream_of=ctx.downstream_of or "",
            selected_upstream_of=ctx.upstream_of or "",
            selected_only=list(ctx.only_steps),
            selected_phases=list(ctx.phases),
            selected_blocks=list(ctx.blocks),
            skipped_blocks=list(ctx.skip_blocks),
            run_dir=run_dir,
            artifacts_dir=str(report.get("artifacts_dir") or ""),
            logs_dir=str(report.get("logs_dir") or ""),
            events_path=str(report.get("events_path") or ""),
            plan_path=str(pipeline_path),
            report_path=job.report_path or (str(Path(run_dir) / "report.json") if run_dir else ""),
            trace_path=str(Path(run_dir) / "semantic-trace.json") if run_dir else "",
        )
        self._write_semantic_trace(run, workflow, plan)
        return run

    @staticmethod
    def _results(report: dict[str, Any], plan: ExecutionPlan) -> list[StepResult]:
        nodes = {node.id: node for node in plan.nodes}
        out: list[StepResult] = []
        for raw in report.get("results", []):
            if not isinstance(raw, dict):
                continue
            step_id = str(raw.get("step_id", ""))
            data = dict(raw.get("data") or {}) if isinstance(raw.get("data"), dict) else {}
            node = nodes.get(step_id)
            original_type = str(data.get("styler_step_type") or (node.step.step_type if node else raw.get("type", "plugin")))
            status = str(
                data.get("styler_status")
                or raw.get("status")
                or (Status.OK if raw.get("success") else Status.FAILED)
            )
            result = StepResult(
                step_id=step_id,
                step_type=original_type,
                success=bool(raw.get("success", False)),
                status=status,
                message=str(raw.get("message", "")),
                output=str(raw.get("output", "")),
                data=data,
                attempts=int(data.get("attempts", 1) or 1),
                duration_ms=int(data.get("duration_ms", data.get("total_duration_ms", 0)) or 0),
            )
            if node is not None:
                result.with_node(node)
            out.append(result)
        return out

    @staticmethod
    def _write_semantic_trace(run: WorkflowRun, workflow: WorkflowDefinition, plan: ExecutionPlan) -> None:
        if not run.trace_path:
            return
        path = Path(run.trace_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            results = {r.node_id: r for r in run.results}
            trace = {
                "schema": "styler.semantic-trace/3",
                "runtime": "pipecraft/1.5",
                "run_id": run.run_id,
                "workflow": workflow.name,
                "operation": workflow.operation,
                "pipeline_fingerprint": run.pipeline_fingerprint,
                "plan_fingerprint": run.plan_fingerprint,
                "planned_order": topological_order(plan),
                "nodes": [],
            }
            for pos, node_id in enumerate(topological_order(plan), 1):
                node = plan.node(node_id); result = results.get(node_id)
                if node is None: continue
                trace["nodes"].append({
                    "planned_position": pos,
                    "node_id": node.id,
                    "source_step_id": node.source_id,
                    "step_type": node.step.step_type,
                    "phase": node.phase,
                    "block": node.block,
                    "needs": list(node.needs),
                    "status": result.status if result else "not_selected",
                    "success": result.success if result else False,
                })
            path.write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            # El report durable de PipeCraft sigue siendo fuente de ejecución;
            # la traza semántica es diagnóstico de Styler.
            pass
