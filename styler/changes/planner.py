from __future__ import annotations

from ._service_support import *  # noqa: F401,F403
from ._service_support import _CONFIG_VERSION_DIR, _change_name

class PlanningOperations:
    def build_plan(
        self,
        change_id: str,
        provider_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ChangePlan:
        self._require_change(change_id)
        if self._is_portable_change(change_id):
            return self._build_portable_plan(change_id)
        if change_id in self._declarative_changes:
            return self._build_declarative_plan(change_id)
        provider_id = provider_id or self.provider_for(change_id)
        resolved_options = self.normalize_options(change_id, options)
        option = self._provider_option(provider_id)
        continuation_mode = self._continuation_mode(change_id)
        if provider_id == "flatpak":
            workflow = self._build_automatic_photogimp(provider_id)
            workflow = self._with_initial_checkpoint(workflow, change_id)
            workflow = self._apply_options(workflow, resolved_options)
            automatic = True
            summary = (
                "Styler instalará GIMP desde Flathub, lo abrirá una vez, respaldará su "
                "configuración, integrará la última publicación de PhotoGIMP y volverá "
                "a abrir GIMP para confirmar que arranca adaptado."
            )
            notice = "Flathub es la estrategia recomendada y comprobada para PhotoGIMP."
            level = AutomationLevel.AUTOMATIC
        else:
            workflow = self._build_assisted_photogimp(provider_id)
            workflow = self._with_initial_checkpoint(workflow, change_id)
            automatic = False
            summary = (
                f"Styler instalará GIMP mediante {option.label} y descargará PhotoGIMP en "
                "la carpeta de Descargas. La copia final deberá hacerse manualmente."
            )
            notice = option.warning
            level = AutomationLevel.ASSISTED

        reconciliation_context = ExecutionContext(
            root=self.root,
            dry_run=True,
            approve=False,
            values={
                "home": str(self.home),
                "change_id": change_id,
                "continuation_mode": continuation_mode,
            },
        )
        reconciled_results = workflow_runtime.WorkflowPlanner(extended_registry()).reconcile(
            workflow, reconciliation_context
        )
        reconciled_steps = {
            step_id: {
                "status": result.status,
                "message": result.message,
                "data": dict(result.data),
            }
            for step_id, result in reconciled_results.items()
        }
        phases = self._phases_for_workflow(workflow, automatic=automatic)
        phases = self._decorate_reconciled_phases(phases, reconciled_results)

        if "app.gimp.install" in reconciled_results:
            if automatic:
                summary = (
                    "GIMP ya está instalado y no se volverá a instalar. Styler continuará "
                    "desde la inicialización o desde el primer paso que siga pendiente, "
                    "respaldará la configuración e integrará PhotoGIMP."
                )
            else:
                summary = (
                    f"GIMP ya está instalado y se reutilizará. Styler continuará con la "
                    f"preparación de PhotoGIMP para {option.label}."
                )
        if continuation_mode and reconciled_results:
            notice = self._join_notice(
                notice,
                f"Continuación detectada: {len(reconciled_results)} paso(s) ya completado(s) "
                "se reutilizarán sin repetir sus efectos.",
            )

        weights = {phase.step_id: phase.weight for phase in phases}
        labels = {phase.step_id: phase.label for phase in phases}
        workflow.operation = "apply"
        workflow.metadata.update(
            {
                "max_workers": 1,
                "change_id": change_id,
                "change_name": "PhotoGIMP",
                "progress_weights": weights,
                "progress_labels": labels,
                "options": dict(resolved_options),
                "continuation_mode": continuation_mode,
                "reconciled_steps": reconciled_steps,
                "dag_role": "apply",
            }
        )
        workflow = annotate_workflow_methods(
            workflow,
            registry=self._method_registry,
            context=MethodContext.detect(),
            policy=self._method_policy,
        )
        undo_workflow = self.rollback_plan(change_id) if self.can_rollback(change_id) else None
        return ChangePlan(
            change_id=change_id,
            name="PhotoGIMP",
            provider_id=provider_id,
            provider_label=option.label,
            automation_level=level,
            summary=summary,
            notice=notice,
            phases=phases,
            workflow=workflow,
            undo_workflow=undo_workflow,
            options=resolved_options,
            reconciled_steps=reconciled_steps,
            continuation_mode=continuation_mode,
            operation="apply",
        )
    def build_removal_plan(self, change_id: str) -> ChangePlan:
        """Construye el DAG exacto para retirar un cambio instalado.

        El plan no invierte el Apply DAG. Se compila desde recibos de efectos
        reales: archivos creados, archivos sustituidos, respaldos y paquetes
        que Styler demostró haber instalado.
        """
        self._validate_change_id(change_id)
        if not self.can_rollback(change_id):
            raise ValueError(
                "Styler no tiene efectos vivos registrados para quitar este cambio. "
                "No se ejecutará una desinstalación por conjetura."
            )

        workflow = self.rollback_plan(change_id)
        record = read_json(self._records_path).get(change_id, {})
        name = str(record.get("name") or _change_name(change_id))
        provider_id = str(record.get("provider_id") or "recorded")
        provider_label = str(
            record.get("provider_label") or PROVIDER_LABELS.get(provider_id, "Registrado por Styler")
        )
        phases = self._phases_for_removal(workflow)
        weights = {phase.step_id: phase.weight for phase in phases}
        labels = {phase.step_id: phase.label for phase in phases}
        workflow.operation = "undo"
        workflow.metadata.update(
            {
                "max_workers": 4,
                "change_id": change_id,
                "change_name": name,
                "progress_weights": weights,
                "progress_labels": labels,
                "dag_role": "undo",
                "user_intent": "remove-change",
            }
        )

        summary, notice = self._removal_summary(workflow)
        return ChangePlan(
            change_id=change_id,
            name=name,
            provider_id=provider_id,
            provider_label=provider_label,
            automation_level=str(
                record.get("automation_level") or AutomationLevel.AUTOMATIC
            ),
            summary=summary,
            notice=notice,
            phases=phases,
            workflow=workflow,
            undo_workflow=workflow,
            options={},
            reconciled_steps={},
            continuation_mode=False,
            operation="remove",
        )

    @staticmethod
    def _workflow_requires_admin(workflow: WorkflowDefinition) -> bool:
        """True cuando el DAG contiene una operación de sistema que necesita root.

        No cambia el DAG ni elige otro proveedor; solo permite que la interfaz
        solicite autorización antes de que ``sudo -n`` tenga que fallar.
        """
        privileged_managers = {"apt", "pacman", "aur", "rpm", "zypper", "snap"}
        for step in workflow.steps:
            satisfied_by = step.config.get("satisfied_by")
            if isinstance(satisfied_by, dict):
                executable = str(satisfied_by.get("executable") or "").strip()
                if executable and shutil.which(executable):
                    # El YAML declaró explícitamente que esta capacidad existente
                    # satisface el paso. No se pedirá sudo para una instalación
                    # que el ejecutor reconciliará sin tocar el sistema.
                    continue
            if step.step_type == "install_package":
                package = dict(step.config.get("package") or {})
                if str(package.get("manager") or "") in privileged_managers:
                    return True
            elif step.step_type == "install_package_artifact":
                if str(step.config.get("manager") or "") in privileged_managers:
                    return True
            elif step.step_type == "uninstall_package":
                if str(step.config.get("manager") or "") in privileged_managers:
                    return True
            elif step.step_type == "enable_service":
                service = dict(step.config.get("service") or {})
                if str(service.get("scope") or "user") != "user":
                    return True
        return False

    def plan_requires_admin(self, plan: ChangePlan) -> bool:
        return os.geteuid() != 0 and self._workflow_requires_admin(plan.workflow)

    def _order_batch_change_ids(self, change_ids: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        """Orden estable del lote con dependencias YAML antes de consumidores.

        Los DAG portables se mantienen como unidades aisladas y conservan el
        orden elegido por la persona. Para YAML, si la dependencia también fue
        seleccionada explícitamente, se adelanta antes de su consumidor.
        """
        requested: list[str] = []
        for raw in change_ids:
            change_id = str(raw)
            self._require_change(change_id)
            if change_id not in requested:
                requested.append(change_id)
        if not requested:
            raise ValueError("Selecciona al menos un cambio para integrar.")

        selected = set(requested)
        ordered: list[str] = []
        visiting: set[str] = set()

        def visit(change_id: str) -> None:
            if change_id in ordered:
                return
            if change_id in visiting:
                raise ValueError(f"Ciclo de dependencias al preparar el lote: {change_id}.")
            visiting.add(change_id)
            declarative = self._declarative_changes.get(change_id)
            if declarative is not None:
                for dependency in declarative.requires_changes:
                    if dependency in selected:
                        visit(dependency)
            visiting.remove(change_id)
            ordered.append(change_id)

        for change_id in requested:
            visit(change_id)
        return tuple(ordered)

    def build_batch_plan(self, change_ids: tuple[str, ...] | list[str]) -> ChangeBatchPlan:
        """Prepara una revisión única sin fusionar ni reescribir los DAG.

        La ejecución posterior reconstruye cada plan justo antes de correrlo.
        Esa reconciliación tardía es deliberada: el cambio N puede satisfacer
        una capacidad que el cambio N+1 necesita y así evita repetir trabajo.
        """
        ordered = self._order_batch_change_ids(change_ids)
        preview_plans: list[ChangePlan] = []
        scheduled: set[str] = set()
        for change_id in ordered:
            plan = self.build_plan(change_id)
            declarative = self._declarative_changes.get(change_id)
            if declarative is not None:
                prior_dependencies = [
                    dependency
                    for dependency in dependency_order(change_id, self._declarative_changes)[:-1]
                    if dependency in scheduled
                ]
                if prior_dependencies:
                    prefixes = tuple(f"yaml.{dependency}." for dependency in prior_dependencies)
                    visible_phases = tuple(
                        phase for phase in plan.phases
                        if not phase.step_id.startswith(prefixes)
                    )
                    dependency_names = [
                        self._declarative_changes[item].recipe.name for item in prior_dependencies
                    ]
                    reuse_notice = (
                        "Dependencia ya programada antes en este lote: "
                        + ", ".join(dependency_names)
                        + ". Al llegar aquí Styler reconstruirá el plan y reutilizará el estado comprobado."
                    )
                    plan = replace(
                        plan,
                        phases=visible_phases,
                        notice=self._join_notice(plan.notice, reuse_notice),
                    )
            preview_plans.append(plan)
            scheduled.add(change_id)
        plans = tuple(preview_plans)
        names = [plan.name for plan in plans]
        summary = (
            f"Styler integrará {len(plans)} cambio(s) de forma secuencial: "
            + " → ".join(names)
            + ". Cada DAG conserva su identidad y se reconciliará de nuevo justo antes de ejecutarse."
        )
        notice = (
            "Si un cambio falla, el lote se detiene antes de iniciar los siguientes. "
            "Los cambios ya completados conservan sus recibos y pueden retirarse individualmente."
        )
        return ChangeBatchPlan(
            change_ids=ordered,
            plans=plans,
            name=f"Lote de {len(plans)} cambios",
            summary=summary,
            notice=notice,
            operation="apply",
        )

    def batch_requires_admin(self, batch: ChangeBatchPlan) -> bool:
        return any(self.plan_requires_admin(plan) for plan in batch.plans)

    def _with_initial_checkpoint(
        self,
        workflow: WorkflowDefinition,
        change_id: str,
    ) -> WorkflowDefinition:
        paths: list[str] = []
        packages: list[dict[str, str]] = []
        effectful_types = {
            "install_package",
            "initialize_flatpak_app",
            "backup_config",
            "install_overlay",
            "apply_config",
            "prepare_manual_handoff",
            "install_package_artifact",
            "integrate_appimage",
        }
        for step in workflow.steps:
            if step.step_type == "install_package":
                package = dict(step.config.get("package") or {})
                manager = str(package.get("manager") or "")
                name = str(package.get("name") or "")
                if manager and name:
                    packages.append({"manager": manager, "name": name})
            for key in ("backup_source", "target", "config_root"):
                value = str(step.config.get(key) or "")
                if value and value not in paths:
                    paths.append(value)

        checkpoint = StepDefinition(
            id="change.checkpoint",
            step_type="create_change_checkpoint",
            description="Crear punto de retorno inicial",
            needs=[],
            phase="checkpoint",
            block=change_id,
            tags=["checkpoint", "reversible"],
            barrier=True,
            config={
                "checkpoint_id": f"{change_id}-initial",
                "scope": "change",
                "paths": paths,
                "packages": packages,
            },
        )
        steps: list[StepDefinition] = [checkpoint]
        for step in workflow.steps:
            if step.step_type in effectful_types and checkpoint.id not in step.needs:
                step.needs = [checkpoint.id, *step.needs]
            steps.append(step)
        phases = dict(workflow.phases)
        phases.setdefault("checkpoint", PhaseDefinition("Crear punto de retorno"))
        return WorkflowDefinition(
            name=workflow.name,
            steps=steps,
            description=workflow.description,
            operation=workflow.operation,
            metadata=dict(workflow.metadata),
            on_error=workflow.on_error,
            phases=phases,
            hooks=workflow.hooks,
            dependency_mode=workflow.dependency_mode,
            observations=dict(workflow.observations),
            outputs=dict(workflow.outputs),
            schema_version=workflow.schema_version,
        )

    def workflow_pair(
        self,
        change_id: str,
        provider_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ChangeWorkflowPair:
        """Devuelve los dos DAG sin fingir que uno es el otro al revés."""
        plan = self.build_plan(change_id, provider_id, options)
        undo = plan.undo_workflow
        return ChangeWorkflowPair(
            change_id=change_id,
            apply=plan.workflow,
            undo=undo,
            undo_available=undo is not None and self.can_rollback(change_id),
            undo_source="receipts",
        )

    def _build_automatic_photogimp(self, provider_id: str) -> WorkflowDefinition:
        resolution = resolve(
            self._registry,
            ["app.photogimp"],
            family=self._target.family or "*",
            preferred_providers={"app.gimp": provider_id},
        )
        compiled = compile_workflow(self._registry, resolution, name="integrate-photogimp")
        if not compiled.ok:
            raise ValueError("; ".join(issue.message for issue in compiled.errors))
        return self._with_final_launch(compiled.workflow)

    @staticmethod
    def _with_final_launch(workflow: WorkflowDefinition) -> WorkflowDefinition:
        """Añade el último paso del instructivo oficial: «Open GIMP».

        Comprobar el marcador y el manifiesto demuestra que los archivos están
        donde deben, no que GIMP arranque con ellos. Una configuración copiada
        con precisión puede seguir impidiendo el arranque, así que el cambio no
        se declara integrado hasta que GIMP se abrió con PhotoGIMP aplicado.
        """
        initialize = next(
            (step for step in workflow.steps if step.id == "app.gimp.initialize"),
            None,
        )
        verify = next(
            (step for step in workflow.steps if step.id == "app.photogimp.verify"),
            None,
        )
        if initialize is None or verify is None:
            return workflow

        config = dict(initialize.config)
        config["semantic_operations"] = [
            {"operation": "application.launch", "label": "Abrir GIMP ya adaptado por PhotoGIMP"},
            {"operation": "wait.observable", "label": "Esperar una ventana estable con la nueva configuración"},
            {"operation": "application.stop", "label": "Cerrar GIMP tras confirmar el arranque"},
            {"operation": "wait.observable", "label": "Esperar el cierre y que la configuración deje de cambiar"},
        ]
        launch_step = StepDefinition(
            id="app.photogimp.launch",
            step_type="initialize_flatpak_app",
            description="Abrir GIMP para confirmar que arranca con PhotoGIMP aplicado",
            needs=[verify.id],
            phase="verify",
            block="app.photogimp",
            tags=["application_overlay", "verification"],
            required=True,
            provider=initialize.provider,
            timeout=initialize.timeout,
            exclusive_resources=["user-config:gimp"],
            config=config,
        )
        return WorkflowDefinition(
            name=workflow.name,
            description=workflow.description,
            steps=[*workflow.steps, launch_step],
            metadata=dict(workflow.metadata),
        )

    def _build_assisted_photogimp(self, provider_id: str) -> WorkflowDefinition:
        resolution = resolve(
            self._registry,
            ["app.gimp"],
            family=self._target.family or "*",
            preferred_providers={"app.gimp": provider_id},
        )
        compiled = compile_workflow(self._registry, resolution, name="prepare-photogimp-manual")
        if not compiled.ok:
            raise ValueError("; ".join(issue.message for issue in compiled.errors))
        photogimp = self._registry.get("app.photogimp")
        source = ""
        if photogimp is not None:
            source = next((provider.source for provider in photogimp.providers if provider.source), "")
        if not source.startswith(PHOTOGIMP_RELEASE_PREFIX):
            raise ValueError("El catálogo no contiene una fuente oficial válida de PhotoGIMP.")
        steps = list(compiled.workflow.steps)
        steps.append(
            StepDefinition(
                id="app.photogimp.handoff",
                step_type="prepare_manual_handoff",
                description="Descargar PhotoGIMP y preparar la integración manual",
                needs=["app.gimp.verify"],
                phase="handoff",
                provider=provider_id,
                shared_resources=["network"],
                config={
                    "source": source,
                    "change_name": "PhotoGIMP",
                    "provider_label": PROVIDER_LABELS.get(provider_id, provider_id),
                },
            )
        )
        return WorkflowDefinition(
            name=compiled.workflow.name,
            description="Preparación asistida de PhotoGIMP",
            steps=steps,
            metadata=dict(compiled.workflow.metadata),
        )

    def _phases_for_workflow(self, workflow: WorkflowDefinition, automatic: bool) -> tuple[ChangePhase, ...]:
        automatic_meta = {
            "change.checkpoint": ("Creando punto de retorno", "Guardando el estado inicial reversible.", 5),
            "app.gimp.install": ("Instalando GIMP", "Descargando e instalando GIMP desde Flathub.", 23),
            "app.gimp.resolve-facts": ("Detectando versión de GIMP", "Consultando versión, rama y carpeta de configuración real.", 4),
            "app.gimp.initialize": ("Iniciando GIMP", "Abriendo GIMP para crear la configuración de la versión detectada.", 10),
            "app.gimp.verify": ("Verificando GIMP", "Comprobando que GIMP quedó disponible.", 5),
            "app.photogimp.backup": ("Protegiendo tu configuración", "Creando un respaldo antes de modificar GIMP.", 10),
            "app.photogimp.install": ("Integrando PhotoGIMP", "Descargando, adaptando y copiando PhotoGIMP.", 35),
            "app.photogimp.verify": ("Verificando PhotoGIMP", "Comprobando el marcador y la carpeta destino.", 15),
            "app.photogimp.launch": (
                "Abriendo GIMP con PhotoGIMP",
                "Confirmando que GIMP arranca con la nueva configuración.",
                12,
            ),
        }
        assisted_meta = {
            "change.checkpoint": ("Creando punto de retorno", "Guardando el estado inicial reversible.", 5),
            "app.gimp.install": ("Instalando GIMP", "Instalando GIMP desde la fuente elegida.", 45),
            "app.gimp.verify": ("Verificando GIMP", "Comprobando que GIMP quedó disponible.", 10),
            "app.photogimp.handoff": ("Preparando PhotoGIMP", "Descargando el archivo y creando instrucciones.", 45),
        }
        metadata = automatic_meta if automatic else assisted_meta
        phases: list[ChangePhase] = []
        by_id = {step.id: step for step in workflow.steps}
        for step_id in topological_order(workflow.steps):
            step = by_id[step_id]
            label, description, weight = metadata.get(
                step.id,
                (step.description or step.id, step.description, 5),
            )
            phases.append(
                ChangePhase(
                    phase_id=step.id.replace(".", "-"),
                    label=label,
                    description=description,
                    weight=float(weight),
                    step_id=step.id,
                    determinate=step.step_type in {"initialize_flatpak_app", "prepare_manual_handoff"},
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
    def _required_packages(workflow: WorkflowDefinition) -> list[dict[str, str]]:
        """Paquetes que el cambio necesita, aunque ya estuvieran instalados."""
        required: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for step in workflow.steps:
            if step.step_type == "install_package":
                package = dict(step.config.get("package") or {})
                manager = str(package.get("manager") or "")
                name = str(package.get("name") or "")
            elif step.step_type == "install_package_artifact":
                manager = str(step.config.get("manager") or "")
                name = str(step.config.get("package_name") or "")
            else:
                continue
            key = (manager, name)
            if not manager or not name or key in seen:
                continue
            seen.add(key)
            required.append({"manager": manager, "package": name})
        return required

    def _package_protections(self, *, excluding_change_id: str) -> dict[str, list[str]]:
        """Cambios activos que todavía declaran necesitar cada paquete."""
        protections: dict[str, list[str]] = {}
        for other_id, raw in read_json(self._records_path).items():
            if other_id == excluding_change_id or not isinstance(raw, dict):
                continue
            status = str(raw.get("status") or ChangeStatus.UNKNOWN)
            if status in {ChangeStatus.REVERTED, ChangeStatus.FAILED}:
                continue
            required = raw.get("required_packages") or []
            if not isinstance(required, list):
                continue
            for item in required:
                if not isinstance(item, dict):
                    continue
                manager = str(item.get("manager") or "")
                package = str(item.get("package") or "")
                if not manager or not package:
                    continue
                protections.setdefault(f"{manager}:{package}", []).append(str(other_id))
        return protections

    def _build_declarative_plan(self, change_id: str) -> ChangePlan:
        change = self._declarative_changes.get(change_id)
        if change is None:
            raise ValueError(f"El cambio YAML '{change_id}' no está disponible.")
        compatibility_error = change.compatibility_error(
            family=self._target.family, architecture=platform.machine(),
        )
        if compatibility_error:
            raise ValueError(compatibility_error)
        workflow = self._compose_declarative_workflow(change_id)
        continuation_mode = self._continuation_mode(change_id)
        reconciliation_context = ExecutionContext(
            root=self.root, dry_run=True, approve=False,
            values={"home": str(self.home), "change_id": change_id, "continuation_mode": continuation_mode},
        )
        reconciled_results = workflow_runtime.WorkflowPlanner(extended_registry()).reconcile(workflow, reconciliation_context)
        reconciled_steps = {
            step_id: {"status": result.status, "message": result.message, "data": dict(result.data)}
            for step_id, result in reconciled_results.items()
        }
        phases = self._decorate_reconciled_phases(
            self._phases_for_workflow(workflow, automatic=True), reconciled_results
        )
        weights = {phase.step_id: phase.weight for phase in phases}
        labels = {phase.step_id: phase.label for phase in phases}
        workflow.metadata.update({
            "max_workers": 2,
            "change_id": change_id,
            "change_name": change.recipe.name,
            "progress_weights": weights,
            "progress_labels": labels,
            "continuation_mode": continuation_mode,
            "reconciled_steps": reconciled_steps,
            "dag_role": "apply",
            "definition_source": "yaml",
            "definition_file": change.source.name,
        })
        workflow = annotate_workflow_methods(
            workflow, registry=self._method_registry,
            context=MethodContext.detect(), policy=self._method_policy,
        )
        requirements = ", ".join(change.requires_changes)
        notice = f"Definido declarativamente en {change.source.name}."
        if requirements:
            notice += f" Styler incluirá automáticamente: {requirements}."
        undo_workflow = self.rollback_plan(change_id) if self.can_rollback(change_id) else None
        return ChangePlan(
            change_id=change_id, name=change.recipe.name, provider_id="yaml",
            provider_label=change.provider_label, automation_level=AutomationLevel.AUTOMATIC,
            summary=change.description, notice=notice, phases=phases, workflow=workflow,
            undo_workflow=undo_workflow, options={}, reconciled_steps=reconciled_steps,
            continuation_mode=continuation_mode, operation="apply",
        )

    def _compose_declarative_workflow(self, change_id: str) -> WorkflowDefinition:
        """Compone YAML requeridos en un solo DAG sin crear una segunda ruta de ejecución."""
        order = dependency_order(change_id, self._declarative_changes)
        checkpoint = StepDefinition(
            id="change.checkpoint", step_type="create_change_checkpoint",
            description="Registrar el punto inicial antes de modificar el sistema.",
            phase="prepare", config={"scope": "yaml-change", "recipe_id": change_id},
            provides=["change.checkpoint.ready"],
        )
        steps: list[StepDefinition] = [checkpoint]
        previous_terminals = [checkpoint.id]
        phases: dict[str, PhaseDefinition] = {"prepare": PhaseDefinition(description="Preparación y checkpoint")}
        for recipe_id in order:
            recipe = self._declarative_changes[recipe_id].recipe
            compiled = compile_recipe(recipe)
            original = [step for step in compiled.steps if step.id != "change.checkpoint"]
            mapping = {step.id: f"yaml.{recipe_id}.{step.id}" for step in original}
            local_ids = set(mapping)
            depended_on: set[str] = set()
            namespaced: list[StepDefinition] = []
            for step in original:
                needs: list[str] = []
                for need in step.needs:
                    if need == "change.checkpoint":
                        needs.extend(previous_terminals)
                    elif need in mapping:
                        needs.append(mapping[need])
                    else:
                        needs.append(need)
                    if need in local_ids:
                        depended_on.add(need)
                config = dict(step.config)
                if recipe_id != change_id and step.step_type == "install_package_artifact":
                    # Infraestructura compartida (p. ej. AppImageLauncher) no se
                    # desinstala al retirar el cambio consumidor.
                    config["retain_on_rollback"] = True
                namespaced.append(replace(
                    step, id=mapping[step.id], source_id=step.source_id or step.id,
                    needs=list(dict.fromkeys(needs)), block=recipe_id, config=config,
                ))
            steps.extend(namespaced)
            terminals = [mapping[item] for item in mapping if item not in depended_on]
            previous_terminals = terminals or previous_terminals
            phases.update(compiled.phases)
        target = self._declarative_changes[change_id]
        return WorkflowDefinition(
            name=target.recipe.name, description=target.description, operation="apply",
            steps=steps, phases=phases,
            metadata={"recipe_id": change_id, "definition_source": "yaml", "composed_changes": list(order)},
        )

    def _build_portable_plan(self, change_id: str) -> ChangePlan:
        """Envuelve un DAG portable en la experiencia existente de Cambios.

        No recompila, reordena ni sustituye el workflow del paquete. Solo
        construye las fases legibles que ``ChangeReviewScreen`` ya consume.
        """
        source = self._portable_source(change_id)
        if source is None:
            raise ValueError(
                "El paquete que define este cambio ya no está disponible."
            )
        package, graph = source
        workflow = graph.workflow
        phases = self._phases_for_workflow(workflow, automatic=True)
        name = package.manifest.name if self._package_graph_count(package) == 1 else graph.title
        description = graph.description or package.manifest.description
        summary = description or (
            f"Styler aplicará el DAG «{graph.title}» contenido en {package.identity}."
        )
        notice = (
            f"Origen: {package.identity}. El DAG se ejecutará con el mismo motor PipeCraft "
            "que usa Styler para los demás cambios."
        )
        undo_workflow = self.rollback_plan(change_id) if self.can_rollback(change_id) else None
        return ChangePlan(
            change_id=change_id,
            name=name,
            provider_id="stylerpkg",
            provider_label="DAG de paquete .stylerpkg",
            automation_level=AutomationLevel.AUTOMATIC,
            summary=summary,
            notice=notice,
            phases=phases,
            workflow=workflow,
            undo_workflow=undo_workflow,
            options={},
            reconciled_steps={},
            continuation_mode=False,
            operation="apply",
        )
