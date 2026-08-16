//! PipeCraft execution engine.
//!
//! V1.4 is Rust-first and Tokio-native internally. `run_async` is the canonical
//! execution path; `run` remains as a compatibility facade for synchronous Rust
//! callers. Multi-pipeline callers should use `RuntimeManager` so resource and
//! task limits are shared globally.

use std::collections::HashSet;
use std::sync::Arc;

use chrono::Utc;

use pipecraft_core::model::{ErrorPolicy, PipelineDefinition, PipelineStep};
use pipecraft_graph::{topological_order, GraphError, Node};
use pipecraft_report::{status, PipelineRun, StepResult};

use crate::context::ExecutionContext;
use crate::coordinator::RuntimeCoordinator;
use crate::events::emit_event;
use crate::executor::{error_result, ExecutorRegistry};
use crate::scheduler::{plan_fingerprint, schedule, PolicyResolver, StepRunner};

#[derive(Clone)]
pub struct PipelineEngine {
    pub registry: ExecutorRegistry,
}

impl Default for PipelineEngine {
    fn default() -> Self {
        Self {
            registry: ExecutorRegistry::default_v1(),
        }
    }
}

impl PipelineEngine {
    pub fn new(registry: ExecutorRegistry) -> Self {
        Self { registry }
    }

    /// Full validation: static config checks + DAG cycle detection.
    pub fn validate(&self, pipeline: &PipelineDefinition) -> Vec<String> {
        let mut errors = pipecraft_core::validate_static(pipeline, &self.registry.known_types());
        if let Err(GraphError::Cycle { remaining }) = self.compute_order(pipeline) {
            errors.push(format!(
                "dependency cycle detected among steps: {}",
                remaining.join(", ")
            ));
        }
        errors
    }

    fn compute_order(&self, pipeline: &PipelineDefinition) -> Result<Vec<String>, GraphError> {
        let nodes: Vec<Node> = pipeline
            .steps
            .iter()
            .map(|step| Node {
                id: step.id.clone(),
                needs: step.needs.clone(),
            })
            .collect();
        topological_order(&nodes)
    }

    pub fn plan(&self, pipeline: &PipelineDefinition) -> Result<Vec<String>, Vec<String>> {
        let errors = self.validate(pipeline);
        if !errors.is_empty() {
            return Err(errors);
        }
        Ok(self.compute_order(pipeline).unwrap_or_default())
    }

    /// Compatibility facade. New Rust callers that already have a Tokio runtime
    /// should prefer `run_async`.
    pub fn run(&self, pipeline: &PipelineDefinition, ctx: &ExecutionContext) -> PipelineRun {
        let engine = self.clone();
        let pipeline = pipeline.clone();
        let ctx = ctx.clone();

        // Avoid trying to nest a runtime on a Tokio worker. A short helper
        // thread gives synchronous callers deterministic behaviour in either
        // environment without leaking Python or another scheduler into the core.
        if tokio::runtime::Handle::try_current().is_ok() {
            let fallback_pipeline = pipeline.clone();
            let fallback_ctx = ctx.clone();
            return std::thread::spawn(move || {
                build_runtime(ctx.max_workers).block_on(engine.run_async(&pipeline, &ctx))
            })
            .join()
            .unwrap_or_else(|_| {
                panic_run(
                    &fallback_pipeline,
                    &fallback_ctx,
                    "synchronous runtime thread panicked",
                )
            });
        }

        build_runtime(ctx.max_workers).block_on(engine.run_async(&pipeline, &ctx))
    }

    /// Canonical single-pipeline async entry point. It owns a local coordinator;
    /// `RuntimeManager` supplies a shared one when several pipelines run at once.
    pub async fn run_async(
        &self,
        pipeline: &PipelineDefinition,
        ctx: &ExecutionContext,
    ) -> PipelineRun {
        let coordinator = RuntimeCoordinator::new(ctx.max_workers);
        self.run_async_with_coordinator(pipeline, ctx, coordinator).await
    }

    pub async fn run_async_with_coordinator(
        &self,
        pipeline: &PipelineDefinition,
        ctx: &ExecutionContext,
        coordinator: RuntimeCoordinator,
    ) -> PipelineRun {
        let run_id = if ctx.run_id.is_empty() {
            format!(
                "{}-{}-{}",
                Utc::now().format("%Y%m%dT%H%M%SZ"),
                sanitize_run_part(&pipeline.name),
                &uuid::Uuid::new_v4().simple().to_string()[..8]
            )
        } else {
            ctx.run_id.clone()
        };
        let run_ctx = ctx.clone().for_run(run_id.clone());
        let started = Utc::now().to_rfc3339();

        // Fail closed before any executor can mutate the outside world.
        if let Err(error) = prepare_run_storage(&run_ctx).await {
            let result = StepResult::new(
                "__storage__",
                "storage",
                false,
                status::FAILED,
                format!("run storage could not be prepared: {error}"),
            )
            .with_data(serde_json::json!({ "error_code": "RUN_STORAGE_UNAVAILABLE" }));
            return self.finish_run(
                run_id,
                pipeline,
                &run_ctx,
                started,
                vec![result],
                Vec::new(),
                false,
                String::new(),
                coordinator.max_tasks(),
            );
        }

        let validation_errors = self.validate(pipeline);
        if !validation_errors.is_empty() {
            let detail = validation_errors
                .iter()
                .map(|error| format!("- {error}"))
                .collect::<Vec<_>>()
                .join("\n");
            let result = StepResult::new(
                "__validation__",
                "validation",
                false,
                status::FAILED,
                format!(
                    "pipeline validation failed with {} errors",
                    validation_errors.len()
                ),
            )
            .with_output(detail)
            .with_data(serde_json::json!({
                "error_code": "PIPELINE_VALIDATION_FAILED",
                "errors": validation_errors,
            }));
            let _ = emit_event(
                &run_ctx,
                "pipeline_validation_failed",
                None,
                result.data.clone(),
            );
            return self.finish_run(
                run_id,
                pipeline,
                &run_ctx,
                started,
                vec![result],
                Vec::new(),
                false,
                String::new(),
                coordinator.max_tasks(),
            );
        }

        let order = self.compute_order(pipeline).unwrap_or_default();
        let selected = match self.select_order(&order, &run_ctx) {
            Ok(selected) => selected,
            Err(result) => {
                return self.finish_run(
                    run_id,
                    pipeline,
                    &run_ctx,
                    started,
                    vec![result],
                    order,
                    false,
                    String::new(),
                    coordinator.max_tasks(),
                );
            }
        };
        let fingerprint = plan_fingerprint(pipeline, &order);
        let _ = emit_event(
            &run_ctx,
            "pipeline_started",
            None,
            serde_json::json!({
                "pipeline": pipeline.name,
                "dry_run": run_ctx.dry_run,
                "selected": selected,
                "plan_fingerprint": fingerprint,
                "runtime": "rust-tokio",
                "global_max_tasks": coordinator.max_tasks(),
            }),
        );

        let registry = self.registry.clone();
        let pipeline_owned = Arc::new(pipeline.clone());
        let context_owned = Arc::new(run_ctx.clone());
        let execute: StepRunner = Arc::new(move |step: PipelineStep| {
            let registry = registry.clone();
            let pipeline = pipeline_owned.clone();
            let context = context_owned.clone();
            Box::pin(async move {
                execute_step_async(&registry, &step, &pipeline, &context).await
            })
        });

        let error_policy = pipeline.on_error.clone();
        let policy_for: PolicyResolver = Arc::new(move |step, result| {
            resolve_error_policy(&error_policy, step, result)
        });

        let output = schedule(
            pipeline,
            &order,
            &selected,
            &run_ctx,
            &fingerprint,
            execute,
            policy_for,
            coordinator.clone(),
        )
        .await;

        let (results, success) = match output {
            Ok(output) => (output.results, output.success),
            Err(result) => (vec![result], false),
        };
        let _ = emit_event(
            &run_ctx,
            "pipeline_finished",
            None,
            serde_json::json!({
                "success": success,
                "results": results.len(),
                "runtime": "rust-tokio",
            }),
        );
        self.finish_run(
            run_id,
            pipeline,
            &run_ctx,
            started,
            results,
            order,
            success,
            fingerprint,
            coordinator.max_tasks(),
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn finish_run(
        &self,
        run_id: String,
        pipeline: &PipelineDefinition,
        ctx: &ExecutionContext,
        started: String,
        results: Vec<StepResult>,
        order: Vec<String>,
        success: bool,
        fingerprint: String,
        global_max_tasks: usize,
    ) -> PipelineRun {
        PipelineRun {
            run_id,
            pipeline: pipeline.name.clone(),
            success,
            dry_run: ctx.dry_run,
            labels: ctx.labels.clone(),
            started_at: started,
            finished_at: Utc::now().to_rfc3339(),
            results,
            order,
            selected_from: ctx.from_step.clone().unwrap_or_default(),
            selected_only: ctx.only_steps.clone(),
            run_dir: ctx.run_dir.display().to_string(),
            artifacts_dir: ctx.artifacts_dir.display().to_string(),
            logs_dir: ctx.logs_dir.display().to_string(),
            events_path: ctx.events_path.display().to_string(),
            state_path: ctx.state_path.display().to_string(),
            plan_fingerprint: fingerprint,
            max_workers: ctx.max_workers,
            runtime: "rust-tokio".into(),
            global_max_tasks,
            report_path: String::new(),
        }
    }

    fn select_order(
        &self,
        order: &[String],
        ctx: &ExecutionContext,
    ) -> Result<Vec<String>, StepResult> {
        let mut selected = order.to_vec();
        if let Some(from) = &ctx.from_step {
            let Some(position) = selected.iter().position(|id| id == from) else {
                return Err(
                    StepResult::new(
                        "__selection__",
                        "selection",
                        false,
                        status::FAILED,
                        format!("--from step not found: {from}"),
                    )
                    .with_data(serde_json::json!({
                        "error_code": "FROM_STEP_NOT_FOUND",
                        "from": from,
                    })),
                );
            };
            selected = selected[position..].to_vec();
        }
        if !ctx.only_steps.is_empty() {
            let all: HashSet<&String> = order.iter().collect();
            let missing: Vec<String> = ctx
                .only_steps
                .iter()
                .filter(|id| !all.contains(id))
                .cloned()
                .collect();
            if !missing.is_empty() {
                return Err(
                    StepResult::new(
                        "__selection__",
                        "selection",
                        false,
                        status::FAILED,
                        format!("--only references missing steps: {}", missing.join(", ")),
                    )
                    .with_data(serde_json::json!({
                        "error_code": "ONLY_STEP_NOT_FOUND",
                        "missing": missing,
                    })),
                );
            }
            let requested: HashSet<&String> = ctx.only_steps.iter().collect();
            selected.retain(|id| requested.contains(id));
        }
        Ok(selected)
    }
}

async fn execute_step_async(
    registry: &ExecutorRegistry,
    step: &PipelineStep,
    pipeline: &PipelineDefinition,
    ctx: &ExecutionContext,
) -> StepResult {
    let mut result = match registry.get(&step.step_type) {
        None => error_result(
            step,
            format!("unknown step type: {}", step.step_type),
            "UNKNOWN_STEP_TYPE",
            "Register an executor or correct the YAML step type.",
        ),
        Some(_) if step.requires_approval && !ctx.approve => StepResult::new(
            &step.id,
            &step.step_type,
            false,
            status::NEEDS_APPROVAL,
            "step requires approval",
        ),
        Some(executor) => executor.run_async(step, pipeline, ctx).await,
    };

    if !status::is_valid(&result.status) {
        result.set_data(
            "warning",
            serde_json::json!(format!(
                "executor returned unknown status '{}'",
                result.status
            )),
        );
    }
    result
}

fn resolve_error_policy(
    policy: &ErrorPolicy,
    step: &PipelineStep,
    result: &StepResult,
) -> String {
    if let Some(value) = policy.steps.get(&step.id) {
        return value.clone();
    }
    if let Some(value) = policy.types.get(&step.step_type) {
        return value.clone();
    }
    if let Some(value) = policy.statuses.get(&result.status) {
        return value.clone();
    }
    policy.default.clone()
}

async fn prepare_run_storage(ctx: &ExecutionContext) -> std::io::Result<()> {
    tokio::fs::create_dir_all(&ctx.run_dir).await?;
    tokio::fs::create_dir_all(&ctx.artifacts_dir).await?;
    tokio::fs::create_dir_all(&ctx.logs_dir).await?;
    tokio::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&ctx.events_path)
        .await?;
    if !tokio::fs::try_exists(&ctx.state_path).await.unwrap_or(false) {
        let probe = ctx.run_dir.join(".state-write-probe");
        tokio::fs::write(&probe, b"probe").await?;
        tokio::fs::remove_file(probe).await?;
    }
    Ok(())
}

fn build_runtime(max_workers: usize) -> tokio::runtime::Runtime {
    let cores = std::thread::available_parallelism()
        .map(|value| value.get())
        .unwrap_or(2);
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(cores.max(max_workers).max(2))
        .enable_all()
        .build()
        .expect("failed to build PipeCraft Tokio runtime")
}

pub(crate) fn panic_run(
    pipeline: &PipelineDefinition,
    ctx: &ExecutionContext,
    message: &str,
) -> PipelineRun {
    PipelineRun {
        run_id: ctx.run_id.clone(),
        pipeline: pipeline.name.clone(),
        success: false,
        dry_run: ctx.dry_run,
        labels: ctx.labels.clone(),
        started_at: Utc::now().to_rfc3339(),
        finished_at: Utc::now().to_rfc3339(),
        results: vec![StepResult::new(
            "__runtime__",
            "runtime",
            false,
            status::FAILED,
            message,
        )],
        order: Vec::new(),
        selected_from: String::new(),
        selected_only: Vec::new(),
        run_dir: String::new(),
        artifacts_dir: String::new(),
        logs_dir: String::new(),
        events_path: String::new(),
        state_path: String::new(),
        plan_fingerprint: String::new(),
        max_workers: ctx.max_workers,
        runtime: "rust-tokio".into(),
        global_max_tasks: ctx.max_workers,
        report_path: String::new(),
    }
}

fn sanitize_run_part(value: &str) -> String {
    let mut output = String::new();
    for character in value.chars() {
        if character.is_ascii_alphanumeric() || matches!(character, '-' | '_') {
            output.push(character);
        } else {
            output.push('-');
        }
    }
    if output.is_empty() {
        "pipeline".into()
    } else {
        output
    }
}
