from __future__ import annotations

from ._service_support import *  # noqa: F401,F403
from ._service_support import _CONFIG_VERSION_DIR, _change_name

class RemovalOperations:
    def can_rollback(self, change_id: str) -> bool:
        """Hay algo que deshacer solo si quedaron recibos vivos.

        No se promete reversibilidad por el hecho de que el cambio sea
        automático: se promete porque existen efectos registrados.
        """
        try:
            return bool(self.journal_for_change(change_id).pending_undo())
        except (OSError, ValueError):
            return False

    def rollback_plan(self, change_id: str) -> WorkflowDefinition:
        receipts = self.journal_for_change(change_id).pending_undo()
        workflow = compile_rollback_workflow(
            receipts,
            name=f"undo-{change_id}",
            description=f"Reversión de {change_id} compilada desde {len(receipts)} recibo(s).",
            package_protections=self._package_protections(excluding_change_id=change_id),
        )
        workflow.metadata.update({"change_id": change_id, "dag_role": "undo"})
        return annotate_workflow_methods(
            workflow,
            registry=self._method_registry,
            context=MethodContext.detect(),
            policy=self._method_policy,
        )

    def rollback_change(
        self,
        change_id: str,
        progress: ProgressCallback = None,
        *,
        dry_run: bool = False,
    ) -> ChangeExecutionResult:
        """Deshace efectos confirmados y declara con honestidad lo pendiente."""
        self._validate_change_id(change_id)
        journal = self.journal_for_change(change_id)
        receipts = journal.pending_undo()
        record = read_json(self._records_path).get(change_id, {})
        provider_id = str(record.get("provider_id") or "recorded")
        provider_label = str(
            record.get("provider_label") or PROVIDER_LABELS.get(provider_id, "Registrado por Styler")
        )
        name = str(record.get("name") or _change_name(change_id))

        if not receipts:
            return ChangeExecutionResult(
                change_id=change_id,
                name=name,
                ok=False,
                status=str(record.get("status") or ChangeStatus.UNKNOWN),
                title="No hay nada que deshacer",
                message=(
                    "Styler no tiene efectos registrados de este cambio en este equipo. "
                    "Puede que se aplicara antes de que existieran los recibos, o desde otra herramienta."
                ),
                provider_id=provider_id,
                provider_label=provider_label,
                automation_level=str(record.get("automation_level") or AutomationLevel.AUTOMATIC),
                operation="remove",
            )

        removal_plan = self.build_removal_plan(change_id)
        if not dry_run:
            try:
                self._assert_execution_storage_writable(change_id)
            except ChangeStateWriteError as exc:
                return self._state_write_failure_result(
                    plan=removal_plan, exc=exc, after_effects=False,
                    details=(
                        "El retiro no se inició: Styler necesita poder actualizar los recibos "
                        "mientras deshace cada efecto.",
                    ),
                )

        workflow = removal_plan.workflow
        workflow.description = f"Reversión de {name}."
        workflow.metadata.update({
            "max_workers": 4,
            "change_id": change_id,
            "change_name": name,
            "dag_role": "undo",
        })

        if not dry_run:
            try:
                save_record(self._records_path,
                    change_id, {**record, "status": ChangeStatus.REVERTING, "message": "Reversión en curso."}
                )
            except ChangeStateWriteError as exc:
                return self._state_write_failure_result(
                    plan=removal_plan, exc=exc, after_effects=False,
                    details=("No se inició ningún nodo de reversión.",),
                )

        phases = self._phases_for_removal(workflow)
        workflow.metadata.update(
            {
                "progress_weights": {phase.step_id: phase.weight for phase in phases},
                "progress_labels": {phase.step_id: phase.label for phase in phases},
            }
        )
        phase_by_step = {phase.step_id: (index, phase) for index, phase in enumerate(phases, 1)}
        rollback_started = False

        def runtime_progress(raw: dict[str, Any]) -> None:
            nonlocal rollback_started
            rollback_started = True
            if progress is None:
                return
            step_id = str(raw.get("step_id", ""))
            index, phase = phase_by_step.get(
                step_id, (1, ChangePhase(step_id, "Deshaciendo", "", 1, step_id))
            )
            progress(
                ChangeProgressEvent(
                    change_id=change_id,
                    change_name=name,
                    phase_id=phase.phase_id,
                    phase_label=phase.label,
                    operation=str(raw.get("operation") or phase.description),
                    phase_index=index,
                    phase_count=len(phases) or 1,
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

        context = ExecutionContext(
            root=self.root,
            dry_run=dry_run,
            approve=True,
            values={
                "home": str(self.home),
                "progress_callback": runtime_progress,
                "change_id": change_id,
            },
        )
        try:
            run = workflow_runtime.execute(workflow, context, extended_registry())
        except OSError as exc:
            if not is_storage_failure(exc):
                raise
            storage_exc = storage_error(exc, self.root / ".styler" / "runs")
            return self._state_write_failure_result(
                plan=removal_plan,
                exc=storage_exc,
                after_effects=rollback_started,
                details=(
                    "La reversión ya había comenzado; Styler detuvo los pasos restantes para "
                    "no perder la correspondencia entre efectos y recibos."
                    if rollback_started
                    else "No se ejecutó ningún nodo de reversión.",
                ),
            )
        results = [item for item in run.results if not item.step_id.startswith("__")]
        step_by_id = {step.id: step for step in workflow.steps}
        hard_failures = [item for item in results if not item.success]
        incomplete = [
            item for item in results
            if item.success and item.data.get("fully_reverted") is False
        ]

        fully_reverted_ids: set[str] = set()
        for result in results:
            step = step_by_id.get(result.step_id)
            if not step or not result.success or result.data.get("fully_reverted") is not True:
                continue
            receipt_id = str(step.config.get("receipt_id") or "")
            if receipt_id:
                fully_reverted_ids.add(receipt_id)

        if not dry_run and fully_reverted_ids:
            try:
                journal.mark_rolled_back(
                    [item for item in receipts if item.receipt_id in fully_reverted_ids],
                    run_id=run.run_id,
                )
            except OSError as exc:
                storage_exc = storage_error(exc, journal.path)
                return self._state_write_failure_result(
                    plan=removal_plan, exc=storage_exc, after_effects=True,
                    run_report=run.report_path,
                    details=tuple(
                        ("✓ " if item.success else "✕ ") + item.message for item in results
                    ) + (
                        "Los efectos ya revertidos no pudieron marcarse como retirados en el diario.",
                    ),
                )

        remaining = journal.pending_undo() if not dry_run else receipts
        full = bool(results) and not hard_failures and not incomplete and not remaining
        partial = bool(results) and not hard_failures and not full
        ok = full

        if full:
            status = ChangeStatus.REVERTED
            title = "El cambio se deshizo"
            message = "Todos los efectos registrados fueron revertidos y verificados."
        elif partial:
            status = ChangeStatus.PARTIALLY_REVERTED
            title = "La reversión necesita una decisión"
            pending_packages = [
                item for item in remaining if item.kind == ReceiptKind.PACKAGE_INSTALLED
            ]
            if pending_packages:
                message = (
                    "La configuración de PhotoGIMP fue revertida, pero uno o más paquetes "
                    "no pudieron desinstalarse o siguen protegidos por otros cambios activos."
                )
            else:
                message = (
                    "Styler revirtió los efectos que pudo demostrar, pero conservó elementos "
                    "sin respaldo o con contenido ajeno. No afirmó que el equipo volviera por completo."
                )
        else:
            status = ChangeStatus.NEEDS_ATTENTION
            title = "La reversión quedó incompleta"
            message = next(
                (item.message for item in hard_failures),
                "La reversión terminó sin confirmar el resultado.",
            )

        if not dry_run:
            try:
                save_record(self._records_path,
                    change_id,
                    {
                        "status": status,
                        "provider_id": provider_id,
                        "provider_label": provider_label,
                        "automation_level": record.get("automation_level", AutomationLevel.AUTOMATIC),
                        "message": message,
                        "report_path": run.report_path,
                        "reversible": bool(remaining),
                        "pending_receipts": len(remaining),
                    },
                )
            except ChangeStateWriteError as exc:
                return self._state_write_failure_result(
                    plan=removal_plan, exc=exc, after_effects=True,
                    run_report=run.report_path,
                    details=tuple(
                        ("✓ " if item.success else "✕ ") + item.message for item in results
                    ),
                )

        details: list[str] = []
        for item in results:
            if not item.success:
                prefix = "✕ "
            elif item.data.get("fully_reverted") is False:
                prefix = "⚠ "
            else:
                prefix = "✓ "
            details.append(prefix + item.message)

        return ChangeExecutionResult(
            change_id=change_id,
            name=name,
            ok=ok,
            status=status,
            title=title,
            message=message,
            provider_id=provider_id,
            provider_label=provider_label,
            automation_level=str(record.get("automation_level") or AutomationLevel.AUTOMATIC),
            report_path=run.report_path,
            details=tuple(details),
            operation="remove",
        )

    def _phases_for_removal(self, workflow: WorkflowDefinition) -> tuple[ChangePhase, ...]:
        """Convierte el Undo DAG en pasos concretos y legibles."""
        by_id = {step.id: step for step in workflow.steps}
        phases: list[ChangePhase] = []
        for step_id in topological_order(workflow.steps):
            step = by_id[step_id]
            config = step.config
            if step.step_type == "undo_remove_paths":
                created = config.get("created_paths") or []
                overwritten = config.get("overwritten") or []
                directories = config.get("created_directories") or []
                label = "Retirando archivos del cambio"
                description = (
                    f"Eliminar {len(created)} archivo(s) creado(s), restaurar "
                    f"{len(overwritten)} archivo(s) sustituido(s) y retirar "
                    f"{len(directories)} directorio(s) que queden vacíos."
                )
                weight = 30.0
            elif step.step_type == "undo_restore_backup":
                source = str(config.get("source") or "la configuración anterior")
                label = "Restaurando configuración original"
                description = f"Restaurar {source} desde el respaldo completo registrado."
                weight = 25.0
            elif step.step_type == "undo_restore_checkpoint":
                paths = config.get("paths") or []
                label = "Restableciendo el estado inicial"
                description = (
                    f"Comparar y restaurar {len(paths)} ruta(s) según el checkpoint "
                    "creado antes de integrar el cambio."
                )
                weight = 20.0
            elif step.step_type == "uninstall_package":
                package = str(config.get("package") or "la aplicación")
                if bool(config.get("was_present", False)):
                    label = f"Conservando {package}"
                    description = (
                        f"{package} ya existía antes del cambio; Styler no lo desinstalará."
                    )
                elif config.get("protected_by_changes"):
                    protected = ", ".join(str(x) for x in config["protected_by_changes"])
                    label = f"Conservando {package}"
                    description = (
                        f"{package} se conservará porque también lo necesita: {protected}."
                    )
                else:
                    label = f"Desinstalando {package}"
                    description = (
                        f"Desinstalar {package} con {config.get('manager', 'su gestor')} "
                        "porque Styler registró que no existía antes."
                    )
                weight = 20.0
            else:
                label = step.description or "Revisando efecto registrado"
                description = step.description or step.id
                weight = 5.0
            phases.append(
                ChangePhase(
                    phase_id=step.id.replace(".", "-"),
                    label=label,
                    description=description,
                    weight=weight,
                    step_id=step.id,
                    determinate=False,
                )
            )
        total = sum(item.weight for item in phases) or 1.0
        return tuple(
            ChangePhase(
                phase_id=item.phase_id,
                label=item.label,
                description=item.description,
                weight=item.weight / total,
                step_id=item.step_id,
                determinate=item.determinate,
            )
            for item in phases
        )

    @staticmethod
    def _removal_summary(workflow: WorkflowDefinition) -> tuple[str, str]:
        remove_steps = [s for s in workflow.steps if s.step_type == "undo_remove_paths"]
        backups = [s for s in workflow.steps if s.step_type in {"undo_restore_backup", "undo_restore_checkpoint"}]
        packages = [s for s in workflow.steps if s.step_type == "uninstall_package"]
        files_created = sum(len(s.config.get("created_paths") or []) for s in remove_steps)
        files_overwritten = sum(len(s.config.get("overwritten") or []) for s in remove_steps)
        uninstall = [
            str(s.config.get("package") or "")
            for s in packages
            if not s.config.get("was_present") and not s.config.get("protected_by_changes")
        ]
        retained = [
            str(s.config.get("package") or "")
            for s in packages
            if s.config.get("was_present") or s.config.get("protected_by_changes")
        ]
        summary = (
            f"Styler quitará {files_created} archivo(s) creado(s), restaurará "
            f"{files_overwritten} archivo(s) sustituido(s) y aplicará "
            f"{len(backups)} respaldo(s) o checkpoint(s)."
        )
        if uninstall:
            summary += " También desinstalará: " + ", ".join(uninstall) + "."
        if retained:
            summary += " Se conservará: " + ", ".join(retained) + "."
        notice = (
            "El retiro se calcula desde efectos comprobados. Styler no eliminará "
            "archivos ajenos, directorios con contenido posterior ni aplicaciones que "
            "ya existían o que otro cambio todavía necesita."
        )
        return summary, notice

    def can_delete_available(self, change_id: str) -> bool:
        """Solo las fuentes propiedad del usuario se eliminan desde Cambios.

        Hoy esas fuentes son paquetes ``.stylerpkg`` importados o creados por
        el Constructor. Los cambios incorporados con Styler (PhotoGIMP) forman
        parte de la instalación y no se fingen como "ocultables".
        """
        self._validate_change_id(change_id)
        return self._portable_source(change_id) is not None

    def delete_available_change(self, change_id: str) -> str:
        """Elimina físicamente la fuente local de un cambio disponible.

        Si un paquete contiene varios DAG, todos desaparecen juntos porque la
        unidad almacenada es el ``.stylerpkg``. Los recibos de una integración
        previa se conservan: eliminar la fuente no equivale a retirar efectos.
        """
        self._validate_change_id(change_id)
        source = self._portable_source(change_id)
        if source is None:
            if change_id == "photogimp":
                raise ValueError(
                    "PhotoGIMP viene incorporado con Styler; no es un paquete local que pueda eliminarse."
                )
            raise ValueError(f"El cambio '{change_id}' no tiene una fuente local eliminable.")
        package, _graph = source
        package_id = package.manifest.package_id
        name = package.manifest.name or package_id
        self._portable_library.remove_all(package_id)
        return name
