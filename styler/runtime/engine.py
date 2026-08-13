"""Motor de Styler 0.5 sobre planes compilados PipeCraft 1.3.1."""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

from styler.runtime.events import EventWriter
from styler.runtime.executors import ExecutorRegistry
from styler.runtime.graph import topological_order
from styler.runtime.models import (
    ExecutionContext,
    ExecutionPlan,
    NodeKind,
    PipelineSummary,
    PlanNode,
    Status,
    StepDefinition,
    StepResult,
    WorkflowDefinition,
    WorkflowRun,
)
from styler.runtime.plan import compile_workflow, plan_to_dict
from styler.runtime.scheduler import schedule
from styler.runtime.selection import SelectionError, SelectionPreview, preview_selection, select_plan_nodes
from styler.runtime.validation import validate_workflow


class WorkflowEngine:
    def __init__(self, registry: ExecutorRegistry | None = None) -> None:
        self.registry = registry or ExecutorRegistry.default()

    def validate(self, workflow: WorkflowDefinition) -> list[str]:
        errors = validate_workflow(workflow, self.registry.known_types())
        from styler.methods import validate_method_bindings

        errors.extend(validate_method_bindings(workflow))
        return errors

    def compile(self, workflow: WorkflowDefinition) -> ExecutionPlan:
        errors = self.validate(workflow)
        if errors:
            raise ValueError("\n".join(errors))
        return compile_workflow(workflow)

    def plan(self, workflow: WorkflowDefinition) -> list[str]:
        return topological_order(self.compile(workflow))

    def preview(self, workflow: WorkflowDefinition, ctx: ExecutionContext | None = None) -> SelectionPreview:
        return preview_selection(self.compile(workflow), ctx or ExecutionContext())

    def reconciliation(self, workflow: WorkflowDefinition, ctx: ExecutionContext | None = None) -> dict[str, StepResult]:
        """Consulta qué pasos ya están satisfechos en el equipo.

        Esta operación no ejecuta acciones ni escribe estado. Sirve para que la
        revisión del plan pueda distinguir entre «instalar» y «reutilizar lo ya
        completado» antes de que la persona apruebe la ejecución.
        """
        context = ctx or ExecutionContext()
        results: dict[str, StepResult] = {}
        for step_id in topological_order(workflow.steps):
            step = next((item for item in workflow.steps if item.id == step_id), None)
            if step is None:
                continue
            result = self._reconcile_step(step, context)
            if result is not None:
                results[step.id] = result
        return results

    def run(self, workflow: WorkflowDefinition, ctx: ExecutionContext) -> WorkflowRun:
        run_id = ctx.run_id or self._make_run_id(workflow.name)
        run_ctx = ctx.for_run(run_id)
        run_ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
        run_ctx.logs_dir.mkdir(parents=True, exist_ok=True)
        started = _now()

        errors = self.validate(workflow)
        if errors:
            result = StepResult(
                "__validation__",
                "validation",
                False,
                Status.FAILED,
                f"La validación encontró {len(errors)} error(es).",
                output="\n".join(f"- {error}" for error in errors),
                data={"error_code": "WORKFLOW_VALIDATION_FAILED", "errors": errors},
            )
            return self._finish(workflow, run_ctx, started, [result], None, False, "failed")

        try:
            plan = compile_workflow(workflow)
        except Exception as exc:
            result = StepResult(
                "__compile__",
                "compile",
                False,
                Status.FAILED,
                f"No fue posible compilar el plan: {exc}",
                data={"error_code": "PLAN_COMPILATION_FAILED"},
            )
            return self._finish(workflow, run_ctx, started, [result], None, False, "failed")

        run_ctx.plan_path.write_text(
            json.dumps(plan_to_dict(plan), indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        events = EventWriter(run_ctx.events_path, run_id, workflow.name)
        events.emit("pipeline_started", data={
            "operation": workflow.operation,
            "dry_run": run_ctx.dry_run,
            "pipeline_fingerprint": plan.pipeline_fingerprint,
            "plan_fingerprint": plan.plan_fingerprint,
            "dependency_mode": plan.dependency_mode,
        })

        try:
            selected = select_plan_nodes(plan, run_ctx)
        except SelectionError as exc:
            result = StepResult(
                "__selection__",
                "selection",
                False,
                Status.FAILED,
                str(exc),
                data={"error_code": exc.code, **exc.data},
            )
            events.emit("pipeline_finished", result=result, data={"status": "failed"})
            return self._finish(workflow, run_ctx, started, [result], plan, False, "failed")

        if run_ctx.preview:
            results: list[StepResult] = []
            for node_id in topological_order(plan):
                node = plan.node(node_id)
                if node is None:
                    continue
                included = node_id in selected
                result = StepResult(
                    node_id,
                    node.step.step_type,
                    True,
                    Status.PLANNED if included else Status.SKIPPED,
                    "Incluido en la vista previa." if included else "Excluido por la selección.",
                    data={"reason": "selected" if included else "selection_excluded"},
                ).with_node(node)
                results.append(result)
            events.emit("pipeline_finished", data={"status": "planned", "selected": len(selected)})
            return self._finish(workflow, run_ctx, started, results, plan, True, "planned")

        external_progress = run_ctx.values.get("progress_callback")
        raw_weights = dict(workflow.metadata.get("progress_weights", {}) or {})
        raw_labels = dict(workflow.metadata.get("progress_labels", {}) or {})
        progress_lock = threading.Lock()
        execution_order_lock = threading.Lock()
        completed_weight = 0.0
        start_counter = 0
        finish_counter = 0

        def node_weight(node: PlanNode) -> float:
            try:
                return max(0.0, float(raw_weights.get(node.id, raw_weights.get(node.source_id, 1.0))))
            except (TypeError, ValueError):
                return 1.0

        total_weight = sum(node_weight(node) for node in plan.nodes if node.id in selected) or 1.0

        def publish(raw: dict) -> None:
            node_id = str(raw.get("step_id", raw.get("node_id", "")))
            node = plan.node(node_id)
            phase_progress = raw.get("phase_progress")
            with progress_lock:
                base = completed_weight
            weight = node_weight(node) if node else 1.0
            if phase_progress is None:
                total_progress = base / total_weight
            else:
                try:
                    partial = max(0.0, min(1.0, float(phase_progress)))
                except (TypeError, ValueError):
                    partial = 0.0
                total_progress = (base + weight * partial) / total_weight
            payload = dict(raw)
            payload.setdefault("node_id", node_id)
            payload.setdefault("phase_label", raw_labels.get(node_id, raw_labels.get(node.source_id if node else "", node_id)))
            payload.setdefault("phase", node.phase if node else "")
            payload.setdefault("block", node.block if node else "")
            payload["total_progress"] = max(0.0, min(1.0, total_progress))
            events.emit(
                "runtime_event",
                node=node,
                data=payload,
            )
            if callable(external_progress):
                try:
                    external_progress(payload)
                except Exception:
                    pass

        # PipeCraft siempre instala su publicador. Aunque no exista TUI/CLI,
        # los eventos de proceso deben quedar en events.jsonl.
        run_ctx.values["progress_callback"] = publish

        def execute_node(node: PlanNode) -> StepResult:
            nonlocal completed_weight, start_counter, finish_counter
            step = node.step
            with execution_order_lock:
                start_counter += 1
                execution_start_index = start_counter
            execution_started_at = _now()
            publish({
                "node_id": node.id,
                "step_id": node.id,
                "phase_progress": 0.0,
                "status": "running",
                "operation": step.description or node.id,
                "message": "Nodo iniciado.",
            })
            reconciled = self._reconcile_step(step, run_ctx)
            if reconciled is not None:
                result = reconciled
            elif step.requires_approval and not run_ctx.approve:
                result = StepResult(
                    node.id,
                    step.step_type,
                    False,
                    Status.NEEDS_APPROVAL,
                    "El nodo requiere aprobación explícita.",
                )
            else:
                result = self._run_with_retries(step, run_ctx)
            result.with_node(node)
            with execution_order_lock:
                finish_counter += 1
                execution_finish_index = finish_counter
            result.started_at = execution_started_at
            result.finished_at = _now()
            result.duration_ms = int(float(result.data.get("elapsed_seconds", 0.0) or 0.0) * 1000)
            result.data.setdefault("execution_start_index", execution_start_index)
            result.data.setdefault("execution_finish_index", execution_finish_index)
            result.data.setdefault("execution_started_at", execution_started_at)
            result.data.setdefault("execution_finished_at", result.finished_at)
            result.data.setdefault("run_if", node.run_if)
            result.data.setdefault("generated", node.generated)
            with progress_lock:
                completed_weight += node_weight(node)
                final_total = completed_weight / total_weight
            if callable(external_progress):
                try:
                    external_progress({
                        "node_id": node.id,
                        "step_id": node.id,
                        "phase_label": raw_labels.get(node.id, raw_labels.get(node.source_id, node.id)),
                        "phase": node.phase,
                        "block": node.block,
                        "phase_progress": 1.0,
                        "total_progress": max(0.0, min(1.0, final_total)),
                        "status": "completed" if result.success else "failed",
                        "operation": result.message,
                        "message": result.message,
                    })
                except Exception:
                    pass
            return result

        max_workers = int(workflow.metadata.get("max_workers", 4) or 4)
        results, success, pipeline_status = schedule(
            plan,
            selected,
            execute_node,
            run_ctx.run_dir / "state.json",
            policy=workflow.on_error,
            events=events,
            max_workers=max_workers,
            resume=True,
        )
        events.emit("pipeline_finished", data={
            "success": success,
            "status": pipeline_status,
            "summary": _summary(results).__dict__,
        })
        return self._finish(workflow, run_ctx, started, results, plan, success, pipeline_status)

    def _reconcile_step(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        # La reconciliación existe para retomar un intento anterior que quedó a
        # medias, no para saltarse pasos en una ejecución nueva. Sin esta
        # condición, un equipo que ya tuviera rastros del cambio veía cómo el
        # pipeline daba por hechos pasos que nunca corrió en esta ejecución.
        if not bool(ctx.values.get("continuation_mode", False)):
            return None
        executor = self.registry.get(step.step_type)
        if executor is None:
            return None
        try:
            result = executor.reconcile(step, ctx)
        except Exception as exc:
            # Una sonda defectuosa nunca debe impedir el camino normal. El
            # ejecutor real seguirá siendo la fuente final de verdad y dejará
            # un diagnóstico completo si falla.
            ctx.values.setdefault("reconciliation_warnings", []).append(
                {"step_id": step.id, "step_type": step.step_type, "error": str(exc)}
            )
            return None
        if result is None:
            return None
        if not result.success:
            ctx.values.setdefault("reconciliation_warnings", []).append(
                {
                    "step_id": step.id,
                    "step_type": step.step_type,
                    "error": result.message,
                }
            )
            return None
        result.data.setdefault("reconciled", True)
        result.status = Status.RECONCILED
        return result

    def _run_with_retries(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        executor = self.registry.get(step.step_type)
        if executor is None:
            return StepResult.failed(step, f"No existe ejecutor para '{step.step_type}'.", "UNKNOWN_STEP_TYPE")

        attempts = step.retries + 1
        last: StepResult | None = None
        for attempt in range(1, attempts + 1):
            began = time.monotonic()
            try:
                result = executor.run(step, ctx)
            except Exception as exc:
                log_path = ctx.logs_dir / f"{step.id}-exception.log"
                log_path.write_text(traceback.format_exc(), encoding="utf-8")
                result = StepResult.failed(
                    step,
                    f"El ejecutor produjo una excepción: {exc}",
                    "UNEXPECTED_EXECUTOR_ERROR",
                    "Conserva el reporte y el log para crear una prueba de regresión.",
                )
                result.data["log"] = str(log_path)

            elapsed = time.monotonic() - began
            result.data.setdefault("attempt", attempt)
            result.data.setdefault("elapsed_seconds", round(elapsed, 6))
            result.attempts = attempt
            if step.operation:
                result.data.setdefault("operation", step.operation)
            if step.method_id:
                result.data.setdefault("method_id", step.method_id)
                result.data.setdefault("method_reason", step.method_reason)
                result.data.setdefault("method_candidates", [dict(item) for item in step.method_candidates])
            if step.timeout is not None and elapsed > step.timeout and result.success:
                result = StepResult(
                    step.id,
                    step.step_type,
                    False,
                    Status.TIMEOUT,
                    f"El nodo excedió su timeout de {step.timeout} segundos.",
                    data={"attempt": attempt, "elapsed_seconds": round(elapsed, 6)},
                    attempts=attempt,
                )
            if result.status not in Status.ALL:
                result.data["warning"] = f"Estado desconocido: {result.status}"
            last = result
            if result.success:
                break
            if attempt < attempts and step.retry_delay:
                time.sleep(step.retry_delay)

        assert last is not None
        last.data["attempts"] = last.attempts
        return last

    def _finish(
        self,
        workflow: WorkflowDefinition,
        ctx: ExecutionContext,
        started: str,
        results: list[StepResult],
        plan: ExecutionPlan | None,
        success: bool,
        pipeline_status: str,
    ) -> WorkflowRun:
        order = topological_order(plan) if plan is not None else []
        run = WorkflowRun(
            run_id=ctx.run_id,
            workflow=workflow.name,
            success=success,
            dry_run=ctx.dry_run,
            started_at=started,
            finished_at=_now(),
            results=results,
            operation=workflow.operation,
            status=pipeline_status,
            order=order,
            pipeline_fingerprint=plan.pipeline_fingerprint if plan else "",
            plan_fingerprint=plan.plan_fingerprint if plan else "",
            summary=_summary(results),
            selected_from=ctx.from_step or "",
            selected_downstream_of=ctx.downstream_of or "",
            selected_upstream_of=ctx.upstream_of or "",
            selected_only=list(ctx.only_steps),
            selected_phases=list(ctx.phases),
            selected_blocks=list(ctx.blocks),
            skipped_blocks=list(ctx.skip_blocks),
            run_dir=str(ctx.run_dir),
            artifacts_dir=str(ctx.artifacts_dir),
            logs_dir=str(ctx.logs_dir),
            events_path=str(ctx.events_path),
            plan_path=str(ctx.plan_path),
        )
        report_path = ctx.run_dir / "report.json"
        trace_path = ctx.run_dir / "semantic-trace.json"
        run.report_path = str(report_path)
        run.trace_path = str(trace_path)

        node_by_id = {node.id: node for node in plan.nodes} if plan else {}
        result_by_id = {result.node_id: result for result in results}
        trace = {
            "schema": "styler.semantic-trace/2",
            "run_id": run.run_id,
            "workflow": workflow.name,
            "operation": workflow.operation,
            "pipeline_fingerprint": run.pipeline_fingerprint,
            "plan_fingerprint": run.plan_fingerprint,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "planned_order": list(order),
            "actual_start_order": [],
            "actual_finish_order": [],
            "nodes": [],
        }
        for position, node_id in enumerate(order, 1):
            node = node_by_id.get(node_id)
            result = result_by_id.get(node_id)
            if node is None:
                continue
            trace["nodes"].append({
                "planned_position": position,
                "node_id": node.id,
                "source_step_id": node.source_id,
                "node_kind": node.kind,
                "step_type": node.step.step_type,
                "phase": node.phase,
                "block": node.block,
                "generated": node.generated,
                "run_if": node.run_if,
                "semantic_operation": node.step.operation,
                "method_id": node.step.method_id,
                "method_reason": node.step.method_reason,
                "semantic_sequence": list(node.step.config.get("selected_semantic_sequence") or []),
                "needs": list(node.needs),
                "status": result.status if result else "not_selected",
                "success": result.success if result else False,
                "policy": result.data.get("policy") if result else None,
                "policy_source": result.data.get("policy_source") if result else None,
                "attempts": result.attempts if result else None,
                "duration_ms": result.duration_ms if result else None,
                "execution_start_index": result.data.get("execution_start_index") if result else None,
                "execution_finish_index": result.data.get("execution_finish_index") if result else None,
            })
        executed = [item for item in trace["nodes"] if item.get("execution_start_index") is not None]
        trace["actual_start_order"] = [item["node_id"] for item in sorted(executed, key=lambda item: item["execution_start_index"])]
        trace["actual_finish_order"] = [item["node_id"] for item in sorted(executed, key=lambda item: item["execution_finish_index"])]
        trace_path.write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")
        report_path.write_text(json.dumps(run.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return run

    @staticmethod
    def _make_run_id(name: str) -> str:
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "workflow"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{safe_name}-{uuid.uuid4().hex[:8]}"


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
            summary.succeeded += 1
            summary.warnings += 1
        elif result.success:
            summary.succeeded += 1
        else:
            summary.failed += 1
    return summary


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
