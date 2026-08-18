from __future__ import annotations

from ._service_support import *  # noqa: F401,F403
from ._service_support import _CONFIG_VERSION_DIR, _change_name

class ExecutionOperations:
    def execute_batch(
        self,
        change_ids: tuple[str, ...] | list[str],
        progress: BatchProgressCallback = None,
    ) -> ChangeBatchExecutionResult:
        """Ejecuta los cambios uno por uno usando la ruta normal de ``execute``.

        No existe un segundo ejecutor para lotes. Cada elemento vuelve a pasar
        por ``build_plan``/PipeCraft/recibos. Esto impide colisiones de IDs entre
        DAG importados y permite que el sistema observado tras un cambio sea la
        entrada real del siguiente.
        """
        ordered = self._order_batch_change_ids(change_ids)
        preview_names = {plan.change_id: plan.name for plan in self.build_batch_plan(ordered).plans}
        results: list[ChangeExecutionResult] = []
        skipped_ids: tuple[str, ...] = ()

        for index, change_id in enumerate(ordered, 1):
            def _nested(event: ChangeProgressEvent, *, current_index: int = index) -> None:
                if progress is None:
                    return
                total = ((current_index - 1) + event.total_progress) / len(ordered)
                progress(
                    ChangeBatchProgressEvent(
                        change_id=event.change_id,
                        change_name=event.change_name,
                        change_index=current_index,
                        change_count=len(ordered),
                        change_progress=event.total_progress,
                        total_progress=max(0.0, min(1.0, total)),
                        phase_label=event.phase_label,
                        operation=event.operation,
                        status=event.status,
                        message=event.message,
                        event_type=event.event_type,
                        terminal_line=event.terminal_line,
                        command=event.command,
                        pid=event.pid,
                        elapsed_seconds=event.elapsed_seconds,
                        quiet_seconds=event.quiet_seconds,
                        log_path=event.log_path,
                        returncode=event.returncode,
                    )
                )

            # ``execute`` reconstruye el plan aquí; no reutilizamos el preview.
            result = self.execute(change_id, progress=_nested)
            results.append(result)
            if progress is not None:
                progress(
                    ChangeBatchProgressEvent(
                        change_id=result.change_id,
                        change_name=result.name,
                        change_index=index,
                        change_count=len(ordered),
                        change_progress=1.0 if result.ok else 0.0,
                        total_progress=(index / len(ordered)) if result.ok else ((index - 1) / len(ordered)),
                        phase_label="Cambio completado" if result.ok else "Cambio detenido",
                        operation=result.title,
                        status="completed" if result.ok else "failed",
                        message=result.message,
                    )
                )
            if not result.ok:
                skipped_ids = ordered[index:]
                break

        skipped_names = tuple(preview_names.get(item, _change_name(item)) for item in skipped_ids)
        failed = next((result for result in results if not result.ok), None)
        ok = failed is None and len(results) == len(ordered)
        if ok:
            title = f"Se integraron {len(results)} cambios"
            message = "El lote terminó completo. Cada cambio conservó sus recibos y su estado independiente."
        else:
            completed = sum(1 for result in results if result.ok)
            title = "El lote se detuvo por un fallo"
            if failed is not None:
                message = (
                    f"{completed} cambio(s) se completaron antes del fallo en {failed.name}. "
                    f"{len(skipped_ids)} cambio(s) quedaron sin iniciar."
                )
            else:
                message = "El lote no pudo completarse."
        return ChangeBatchExecutionResult(
            change_ids=ordered,
            results=tuple(results),
            skipped_ids=skipped_ids,
            skipped_names=skipped_names,
            ok=ok,
            title=title,
            message=message,
        )

    def execute(
        self,
        change_id: str,
        provider_id: str | None = None,
        progress: ProgressCallback = None,
        options: dict[str, Any] | None = None,
    ) -> ChangeExecutionResult:
        plan = self.build_plan(change_id, provider_id, options)
        portable_source = self._portable_source(change_id)
        try:
            self._assert_execution_storage_writable(change_id)
            save_record(self._records_path,
                change_id,
                {
                    "status": ChangeStatus.INTEGRATING,
                    "name": plan.name,
                    "provider_id": plan.provider_id,
                    "provider_label": plan.provider_label,
                    "automation_level": plan.automation_level,
                    "required_packages": self._required_packages(plan.workflow),
                    "message": (
                        "Continuando la integración desde los pasos pendientes."
                        if plan.continuation_mode
                        else "Integración en curso."
                    ),
                    "attempt_mode": "continue" if plan.continuation_mode else "fresh",
                },
            )
        except ChangeStateWriteError as exc:
            # No se permite empezar un DAG si Styler ya sabe que no podrá
            # registrar su estado. Esto evita producir efectos huérfanos.
            return self._state_write_failure_result(
                plan=plan, exc=exc, after_effects=False
            )

        phase_by_step = {phase.step_id: (index, phase) for index, phase in enumerate(plan.phases, 1)}
        runtime_started = False

        def runtime_progress(raw: dict[str, Any]) -> None:
            nonlocal runtime_started
            runtime_started = True
            if progress is None:
                return
            step_id = str(raw.get("step_id", ""))
            index, phase = phase_by_step.get(
                step_id,
                (1, ChangePhase(step_id, str(raw.get("phase_label", "Procesando")), "", 1, step_id)),
            )
            progress(
                ChangeProgressEvent(
                    change_id=change_id,
                    change_name=plan.name,
                    phase_id=phase.phase_id,
                    phase_label=phase.label,
                    operation=str(raw.get("operation") or raw.get("message") or phase.description),
                    phase_index=index,
                    phase_count=len(plan.phases),
                    phase_progress=raw.get("phase_progress"),
                    total_progress=max(0.0, min(1.0, float(raw.get("total_progress", 0.0)))),
                    status=str(raw.get("status", "running")),
                    message=str(raw.get("message", "")),
                    event_type=str(raw.get("event_type", "progress")),
                    terminal_line=str(raw.get("terminal_line", "")),
                    command=str(raw.get("command", "")),
                    pid=(int(raw["pid"]) if raw.get("pid") is not None else None),
                    elapsed_seconds=float(raw.get("elapsed_seconds", 0.0) or 0.0),
                    quiet_seconds=float(raw.get("quiet_seconds", 0.0) or 0.0),
                    log_path=str(raw.get("log_path", "")),
                    returncode=(int(raw["returncode"]) if raw.get("returncode") is not None else None),
                )
            )

        def runtime_submitted(run_id: str) -> None:
            save_record(self._records_path,
                change_id,
                {
                    "status": ChangeStatus.INTEGRATING,
                    "pipecraft_run_id": run_id,
                    "runtime_backend": "pipecraft/1.5",
                    "message": "PipeCraft aceptó el DAG y lo está ejecutando.",
                },
            )

        context_values = {
            "home": str(self.home),
            "progress_callback": runtime_progress,
            "run_submitted_callback": runtime_submitted,
            # Habilita los recibos: sin change_id, los ejecutores no
            # registran nada y el cambio no sería reversible.
            "change_id": change_id,
            "options": dict(plan.options),
            "continuation_mode": plan.continuation_mode,
            "reconciled_steps": dict(plan.reconciled_steps),
        }
        if portable_source is not None:
            package, _graph = portable_source
            context_values.update(
                {
                    "package_content_root": Path(package.install_path) / "content",
                    "package_id": package.manifest.package_id,
                    "package_version": package.manifest.version,
                }
            )
        context = ExecutionContext(
            root=self.root,
            dry_run=False,
            approve=True,
            values=context_values,
        )
        sudo_ticket = None
        if self.plan_requires_admin(plan) and shutil.which("sudo"):
            sudo_ticket = keepalive_for(["sudo", "-n"])
            if sudo_ticket is not None and not sudo_ticket.ensure():
                message = (
                    "Styler necesita autorización administrativa antes de ejecutar este DAG. "
                    "No se inició ningún nodo privilegiado."
                )
                save_record(self._records_path,
                    change_id,
                    {
                        "status": ChangeStatus.FAILED,
                        "name": plan.name,
                        "provider_id": plan.provider_id,
                        "provider_label": plan.provider_label,
                        "automation_level": plan.automation_level,
                        "message": message,
                        "required_packages": self._required_packages(plan.workflow),
                        "options": dict(plan.options),
                        "attempt_mode": "continue" if plan.continuation_mode else "fresh",
                    },
                )
                return ChangeExecutionResult(
                    change_id=change_id, name=plan.name, ok=False, status=ChangeStatus.FAILED,
                    title=f"No se pudo iniciar {plan.name}", message=message,
                    provider_id=plan.provider_id, provider_label=plan.provider_label,
                    automation_level=plan.automation_level,
                    details=(
                        "✕ Falta una credencial sudo vigente.",
                        "El DAG se conservó intacto; vuelve a intentarlo y autoriza cuando Styler lo solicite.",
                    ),
                    options=dict(plan.options), operation=plan.operation,
                )
            if sudo_ticket is not None:
                sudo_ticket.start()
        try:
            try:
                run = workflow_runtime.execute(plan.workflow, context, extended_registry())
            except OSError as exc:
                if not is_storage_failure(exc):
                    raise
                storage_exc = storage_error(
                    exc, self.root / ".styler" / "runs"
                )
                return self._state_write_failure_result(
                    plan=plan,
                    exc=storage_exc,
                    after_effects=runtime_started,
                    details=(
                        "PipeCraft ya había iniciado este cambio."
                        if runtime_started
                        else "PipeCraft todavía no había iniciado ningún nodo.",
                    ),
                )
        finally:
            if sudo_ticket is not None:
                sudo_ticket.stop()
        meaningful_results = [result for result in run.results if not result.step_id.startswith("__")]
        ok = bool(meaningful_results) and all(result.success for result in meaningful_results)
        handoff_path = ""
        instructions_path = ""
        for result in meaningful_results:
            handoff_path = str(result.data.get("handoff_path") or handoff_path)
            instructions_path = str(result.data.get("instructions_path") or instructions_path)

        if portable_source is not None and ok:
            status = ChangeStatus.INTEGRATED
            title = f"{plan.name} se integró correctamente"
            message = "El DAG del paquete terminó y Styler confirmó sus pasos requeridos."
        elif portable_source is not None:
            status = ChangeStatus.FAILED
            title = f"No se pudo completar {plan.name}"
            failed = next((item for item in meaningful_results if not item.success), None)
            message = failed.message if failed else "La ejecución terminó sin confirmar el resultado."
        elif ok and plan.automation_level == AutomationLevel.AUTOMATIC:
            status = ChangeStatus.INTEGRATED
            title = "PhotoGIMP se integró correctamente"
            message = "GIMP y PhotoGIMP quedaron instalados, adaptados y verificados."
        elif ok:
            status = ChangeStatus.PREPARED
            title = "PhotoGIMP quedó preparado"
            message = (
                "GIMP quedó instalado y PhotoGIMP fue descargado. La integración final "
                "debe completarse manualmente."
            )
        else:
            status = ChangeStatus.FAILED
            title = "No se pudo completar PhotoGIMP"
            failed = next((item for item in meaningful_results if not item.success), None)
            message = failed.message if failed else "La ejecución terminó sin confirmar el resultado."

        detail_lines: list[str] = []
        for item in meaningful_results:
            prefix = "✓ " if item.success else "✕ "
            detail_lines.append(prefix + item.message)
            if not item.success:
                error_code = str(item.data.get("error_code") or "")
                command = str(item.data.get("command") or "")
                artifact = str(item.data.get("artifact") or "")
                if error_code:
                    detail_lines.append(f"  Código técnico: {error_code}")
                if command:
                    detail_lines.append(f"  Comando: {command}")
                if artifact:
                    detail_lines.append(f"  Log: {artifact}")
        details = tuple(detail_lines)
        try:
            save_record(self._records_path,
                change_id,
                {
                    "status": status,
                    "name": plan.name,
                    "provider_id": plan.provider_id,
                    "provider_label": plan.provider_label,
                    "automation_level": plan.automation_level,
                    "message": message,
                    "report_path": run.report_path,
                    "handoff_path": handoff_path,
                    "instructions_path": instructions_path,
                    "reversible": self.can_rollback(change_id),
                    "required_packages": self._required_packages(plan.workflow),
                    "options": dict(plan.options),
                    "last_run_id": run.run_id,
                    "pipecraft_run_id": run.run_id if ".styler/pipecraft" in str(getattr(run, "run_dir", "")).replace("\\", "/") else "",
                    "runtime_backend": "pipecraft/1.5" if ".styler/pipecraft" in str(getattr(run, "run_dir", "")).replace("\\", "/") else "local-compat",
                    "reconciled_steps": dict(plan.reconciled_steps),
                    "attempt_mode": "continue" if plan.continuation_mode else "fresh",
                },
            )
        except ChangeStateWriteError as exc:
            return self._state_write_failure_result(
                plan=plan,
                exc=exc,
                after_effects=True,
                run_report=run.report_path,
                details=details,
            )
        return ChangeExecutionResult(
            change_id=change_id,
            name=plan.name,
            ok=ok,
            status=status,
            title=title,
            message=message,
            provider_id=plan.provider_id,
            provider_label=plan.provider_label,
            automation_level=plan.automation_level,
            report_path=run.report_path,
            handoff_path=handoff_path,
            instructions_path=instructions_path,
            details=details,
            options=dict(plan.options),
            diagnostic_path=run.report_path,
            operation=plan.operation,
        )

    def _apply_options(
        self, workflow: WorkflowDefinition, options: dict[str, Any]
    ) -> WorkflowDefinition:
        """Las opciones entran al mismo compilador: incluyen o excluyen pasos."""
        if not options.get("backup", True):
            for step in [item for item in workflow.steps if item.step_type == "backup_config"]:
                workflow = drop_step(workflow, step.id)
        timeout = options.get("startup_timeout_seconds")
        for step in workflow.steps:
            if step.step_type == "initialize_flatpak_app" and timeout:
                step.config["startup_timeout_seconds"] = float(timeout)
            if step.step_type in {"install_overlay", "apply_config"}:
                step.config["rewrite_launchers"] = bool(options.get("rewrite_launchers", True))
        return workflow

    def _continuation_mode(self, change_id: str) -> bool:
        record = read_json(self._records_path).get(change_id, {})
        return isinstance(record, dict) and str(record.get("status") or "") in CONTINUATION_STATUSES

    @staticmethod
    def _join_notice(left: str, right: str) -> str:
        return " ".join(part.strip() for part in (left, right) if part and part.strip())

    @staticmethod
    def _decorate_reconciled_phases(
        phases: tuple[ChangePhase, ...],
        reconciled: dict[str, Any],
    ) -> tuple[ChangePhase, ...]:
        labels = {
            "app.gimp.install": "Reutilizando GIMP ya instalado",
            "app.gimp.resolve-facts": "Reutilizando la versión detectada de GIMP",
            "app.gimp.initialize": "Reutilizando la configuración inicializada de GIMP",
            "app.photogimp.backup": "Reutilizando el respaldo anterior",
            "app.photogimp.install": "PhotoGIMP ya copiado; continuar con verificación",
            "app.photogimp.launch": "Reutilizando el arranque ya confirmado de GIMP",
        }
        decorated: list[ChangePhase] = []
        for phase in phases:
            result = reconciled.get(phase.step_id)
            if result is None:
                decorated.append(phase)
                continue
            decorated.append(
                ChangePhase(
                    phase_id=phase.phase_id,
                    label=labels.get(phase.step_id, f"Ya completado · {phase.label}"),
                    description=str(result.message),
                    weight=phase.weight,
                    step_id=phase.step_id,
                    determinate=False,
                )
            )
        return tuple(decorated)
