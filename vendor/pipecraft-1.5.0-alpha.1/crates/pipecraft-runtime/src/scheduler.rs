//! Async DAG scheduler for PipeCraft 1.5.
//!
//! The scheduler is Rust-first and Tokio-native. It coordinates dependencies,
//! conditions, capabilities, per-pipeline worker limits and resources shared
//! across *multiple* concurrent pipelines through `RuntimeCoordinator`.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use tokio::task::JoinSet;

use pipecraft_core::model::{PipelineDefinition, PipelineStep};
use pipecraft_report::{status, StepResult};

use crate::context::ExecutionContext;
use crate::coordinator::RuntimeCoordinator;
use crate::events::emit_event;
use crate::executor::error_result;

pub type OwnedStepFuture = Pin<Box<dyn Future<Output = StepResult> + Send + 'static>>;
pub type StepRunner = Arc<dyn Fn(PipelineStep) -> OwnedStepFuture + Send + Sync>;
pub type PolicyResolver = Arc<dyn Fn(&PipelineStep, &StepResult) -> String + Send + Sync>;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct RunState {
    pub plan_fingerprint: String,
    pub selected: Vec<String>,
    pub dry_run: bool,
    pub labels: Vec<String>,
    pub statuses: BTreeMap<String, String>,
    pub results: BTreeMap<String, StepResult>,
}

impl RunState {
    pub fn load(path: &std::path::Path) -> std::io::Result<Self> {
        let text = std::fs::read_to_string(path)?;
        serde_json::from_str(&text)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))
    }

    pub async fn load_async(path: &std::path::Path) -> std::io::Result<Self> {
        let text = tokio::fs::read_to_string(path).await?;
        serde_json::from_str(&text)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))
    }

    pub fn save(&self, path: &std::path::Path) -> std::io::Result<()> {
        use std::io::Write;
        let tmp = path.with_extension("json.tmp");
        let text = serde_json::to_vec_pretty(self)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
        let mut file = std::fs::File::create(&tmp)?;
        file.write_all(&text)?;
        file.sync_all()?;
        drop(file);
        replace_file_sync(&tmp, path)?;
        sync_parent(path);
        Ok(())
    }

    pub async fn save_async(&self, path: &std::path::Path) -> std::io::Result<()> {
        use tokio::io::AsyncWriteExt;
        let tmp = path.with_extension("json.tmp");
        let text = serde_json::to_vec_pretty(self)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
        let mut file = tokio::fs::File::create(&tmp).await?;
        file.write_all(&text).await?;
        file.flush().await?;
        file.sync_all().await?;
        drop(file);
        if let Err(first) = tokio::fs::rename(&tmp, path).await {
            if tokio::fs::try_exists(path).await.unwrap_or(false) {
                tokio::fs::remove_file(path).await?;
                tokio::fs::rename(&tmp, path).await?;
            } else {
                return Err(first);
            }
        }
        sync_parent(path);
        Ok(())
    }
}

fn replace_file_sync(tmp: &std::path::Path, path: &std::path::Path) -> std::io::Result<()> {
    if let Err(first) = std::fs::rename(tmp, path) {
        if path.exists() {
            std::fs::remove_file(path)?;
            std::fs::rename(tmp, path)?;
        } else {
            return Err(first);
        }
    }
    Ok(())
}

fn sync_parent(path: &std::path::Path) {
    if let Some(parent) = path.parent() {
        if let Ok(dir) = std::fs::File::open(parent) {
            let _ = dir.sync_all();
        }
    }
}

pub struct ScheduleOutput {
    pub results: Vec<StepResult>,
    pub success: bool,
}

pub fn plan_fingerprint(pipeline: &PipelineDefinition, order: &[String]) -> String {
    // Stable FNV-1a 64-bit fingerprint. It is not a security hash; it guards
    // resume against accidentally applying state to a materially different plan.
    let mut hash: u64 = 0xcbf29ce484222325;
    let mut feed = |bytes: &[u8]| {
        for byte in bytes {
            hash ^= *byte as u64;
            hash = hash.wrapping_mul(0x100000001b3);
        }
    };
    feed(pipeline.name.as_bytes());
    for id in order {
        if let Some(step) = pipeline.steps.iter().find(|step| &step.id == id) {
            feed(step.id.as_bytes());
            feed(step.step_type.as_bytes());
            feed(step.description.as_bytes());
            feed(step.repo.as_bytes());
            feed(step.command.as_bytes());
            feed(step.risk.as_bytes());
            feed(step.run_if.as_bytes());
            for value in step
                .needs
                .iter()
                .chain(step.requires.iter())
                .chain(step.provides.iter())
                .chain(step.exclusive_resources.iter())
                .chain(step.shared_resources.iter())
            {
                feed(value.as_bytes());
                feed(&[0]);
            }
            feed(&[
                step.barrier as u8,
                step.required as u8,
                step.requires_approval as u8,
            ]);
            if let Ok(text) = serde_yaml::to_string(&step.rules) {
                feed(text.as_bytes());
            }
            if let Ok(text) = serde_yaml::to_string(&step.with) {
                feed(text.as_bytes());
            }
        }
    }
    format!("fnv1a64:{hash:016x}")
}

fn terminal(value: &str) -> bool {
    !matches!(value, status::PENDING | status::READY | status::RUNNING)
}

fn success_status(value: &str) -> bool {
    matches!(
        value,
        status::OK
            | status::OK_WITH_WARNINGS
            | status::DRY_RUN
            | status::PLANNED
            | status::SUCCEEDED
    )
}

fn failure_status(value: &str) -> bool {
    matches!(
        value,
        status::FAILED
            | status::TIMEOUT
            | status::CANCELLED
            | status::BLOCKED
            | status::NEEDS_APPROVAL
    )
}

fn synthetic(
    step: &PipelineStep,
    status_value: &str,
    success: bool,
    message: &str,
    reason: &str,
) -> StepResult {
    StepResult::new(&step.id, &step.step_type, success, status_value, message)
        .with_data(serde_json::json!({ "reason": reason }))
}

fn dependency_status(state: &RunState, selected: &HashSet<String>, dependency: &str) -> String {
    if !selected.contains(dependency) {
        // Selection flags intentionally allow running a suffix/subset without
        // automatically replaying prerequisites.
        return status::SUCCEEDED.into();
    }
    state
        .statuses
        .get(dependency)
        .cloned()
        .unwrap_or_else(|| status::PENDING.into())
}

async fn load_or_initialize_state(
    selected_order: &[String],
    ctx: &ExecutionContext,
    fingerprint: &str,
) -> Result<RunState, StepResult> {
    let mut state = if ctx.resume && tokio::fs::try_exists(&ctx.state_path).await.unwrap_or(false) {
        match RunState::load_async(&ctx.state_path).await {
            Ok(mut prior)
                if prior.plan_fingerprint == fingerprint
                    && prior.selected.as_slice() == selected_order
                    && prior.dry_run == ctx.dry_run
                    && prior.labels == ctx.labels =>
            {
                for id in selected_order {
                    let current = prior.statuses.get(id).cloned().unwrap_or_default();
                    if !success_status(&current) {
                        prior.statuses.insert(id.clone(), status::PENDING.into());
                        prior.results.remove(id);
                    }
                }
                prior
            }
            Ok(prior) => {
                return Err(
                    StepResult::new(
                        "__resume__",
                        "resume",
                        false,
                        status::FAILED,
                        "resume state does not match the current plan or invocation",
                    )
                    .with_data(serde_json::json!({
                        "error_code": "RESUME_STATE_MISMATCH",
                        "saved_fingerprint": prior.plan_fingerprint,
                        "current_fingerprint": fingerprint,
                        "saved_selected": prior.selected,
                        "current_selected": selected_order,
                        "saved_dry_run": prior.dry_run,
                        "current_dry_run": ctx.dry_run,
                    })),
                );
            }
            Err(error) => {
                return Err(
                    StepResult::new(
                        "__resume__",
                        "resume",
                        false,
                        status::FAILED,
                        format!("could not read resume state: {error}"),
                    )
                    .with_data(serde_json::json!({"error_code":"RESUME_STATE_READ_ERROR"})),
                );
            }
        }
    } else {
        RunState {
            plan_fingerprint: fingerprint.into(),
            selected: selected_order.to_vec(),
            dry_run: ctx.dry_run,
            labels: ctx.labels.clone(),
            ..RunState::default()
        }
    };

    state.plan_fingerprint = fingerprint.into();
    state.selected = selected_order.to_vec();
    state.dry_run = ctx.dry_run;
    state.labels = ctx.labels.clone();
    for id in selected_order {
        state
            .statuses
            .entry(id.clone())
            .or_insert_with(|| status::PENDING.into());
    }
    state.save_async(&ctx.state_path).await.map_err(|error| {
        StepResult::new(
            "__state__",
            "state",
            false,
            status::FAILED,
            format!("could not persist initial run state: {error}"),
        )
        .with_data(serde_json::json!({"error_code":"RUN_STATE_WRITE_ERROR"}))
    })?;
    Ok(state)
}

#[allow(clippy::too_many_arguments)]
pub async fn schedule(
    pipeline: &PipelineDefinition,
    order: &[String],
    selected_order: &[String],
    ctx: &ExecutionContext,
    fingerprint: &str,
    execute: StepRunner,
    policy_for: PolicyResolver,
    coordinator: RuntimeCoordinator,
) -> Result<ScheduleOutput, StepResult> {
    let selected: HashSet<String> = selected_order.iter().cloned().collect();
    let by_id: HashMap<&str, &PipelineStep> = pipeline
        .steps
        .iter()
        .map(|step| (step.id.as_str(), step))
        .collect();
    let mut state = load_or_initialize_state(selected_order, ctx, fingerprint).await?;

    let _ = emit_event(
        ctx,
        "pipeline_scheduled",
        None,
        serde_json::json!({
            "selected": selected_order,
            "max_workers": ctx.max_workers,
            "global_max_tasks": coordinator.max_tasks(),
            "resume": ctx.resume,
            "plan_fingerprint": fingerprint,
            "scheduler": "tokio",
        }),
    );

    let mut running: HashMap<String, bool> = HashMap::new();
    let mut provided: HashSet<String> = HashSet::new();
    for (id, result) in &state.results {
        if result.success {
            if let Some(step) = by_id.get(id.as_str()) {
                provided.extend(step.provides.iter().cloned());
            }
        }
    }

    let mut stop_requested = false;
    let mut hard_failure = false;
    let mut persistence_failed = false;
    let mut cancel_event_emitted = false;
    let mut tasks: JoinSet<(PipelineStep, StepResult)> = JoinSet::new();

    loop {
        let mut changed = false;
        let mut blocked_on_resource: Option<u64> = None;
        let mut blocked_on_task_slot = false;

        if ctx.cancellation.is_cancelled() {
            hard_failure = true;
            stop_requested = true;
            if !cancel_event_emitted {
                let _ = emit_event(ctx, "pipeline_cancel_requested", None, serde_json::json!({}));
                cancel_event_emitted = true;
            }
            for id in selected_order {
                if matches!(
                    state.statuses.get(id).map(String::as_str),
                    Some(status::PENDING | status::READY)
                ) {
                    let step = by_id[id.as_str()];
                    let result = synthetic(
                        step,
                        status::CANCELLED,
                        false,
                        "cancelled before execution",
                        "cancel_requested",
                    );
                    state.statuses.insert(id.clone(), status::CANCELLED.into());
                    state.results.insert(id.clone(), result.clone());
                    let _ = emit_event(ctx, "node_cancelled", Some(id), result.data.clone());
                    changed = true;
                }
            }
        }

        // Resolve dependency conditions for pending nodes.
        for id in order {
            if !selected.contains(id)
                || state.statuses.get(id).map(String::as_str) != Some(status::PENDING)
            {
                continue;
            }
            let step = by_id[id.as_str()];
            let dependencies: Vec<String> = step
                .needs
                .iter()
                .map(|dependency| dependency_status(&state, &selected, dependency))
                .collect();
            if !dependencies.iter().all(|value| terminal(value)) {
                continue;
            }

            if stop_requested && !matches!(step.run_if.as_str(), "always" | "any_failed") {
                let result = synthetic(
                    step,
                    status::BLOCKED,
                    false,
                    "blocked because the pipeline stopped after a failure",
                    "pipeline_stopped",
                );
                state.statuses.insert(id.clone(), status::BLOCKED.into());
                state.results.insert(id.clone(), result.clone());
                hard_failure |= step.required;
                let _ = emit_event(ctx, "node_blocked", Some(id), result.data.clone());
                changed = true;
                continue;
            }

            match step.run_if.as_str() {
                "all_success" => {
                    if dependencies.iter().all(|value| success_status(value)) {
                        state.statuses.insert(id.clone(), status::READY.into());
                    } else {
                        let result = synthetic(
                            step,
                            status::BLOCKED,
                            false,
                            "blocked because a dependency did not succeed",
                            "dependency_failed",
                        );
                        state.statuses.insert(id.clone(), status::BLOCKED.into());
                        state.results.insert(id.clone(), result.clone());
                        hard_failure |= step.required;
                        let _ = emit_event(ctx, "node_blocked", Some(id), result.data.clone());
                    }
                    changed = true;
                }
                "all_complete" | "always" => {
                    state.statuses.insert(id.clone(), status::READY.into());
                    changed = true;
                }
                "any_failed" => {
                    if dependencies.iter().any(|value| failure_status(value)) {
                        state.statuses.insert(id.clone(), status::READY.into());
                    } else {
                        let result = synthetic(
                            step,
                            status::SKIPPED,
                            true,
                            "any_failed condition was not met",
                            "condition_not_met",
                        );
                        state.statuses.insert(id.clone(), status::SKIPPED.into());
                        state.results.insert(id.clone(), result.clone());
                        let _ = emit_event(ctx, "node_skipped", Some(id), result.data.clone());
                    }
                    changed = true;
                }
                _ => unreachable!("run_if validated before scheduling"),
            }
        }

        if changed && state.save_async(&ctx.state_path).await.is_err() {
            persistence_failed = true;
            stop_requested = true;
            hard_failure = true;
        }

        // Launch stable-ready nodes while local/global capacity and resources allow.
        let barrier_running = running.values().any(|barrier| *barrier);
        for id in order {
            if persistence_failed || ctx.cancellation.is_cancelled() {
                break;
            }
            if running.len() >= ctx.max_workers {
                break;
            }
            if !selected.contains(id)
                || running.contains_key(id)
                || state.statuses.get(id).map(String::as_str) != Some(status::READY)
            {
                continue;
            }
            let step = by_id[id.as_str()];

            if stop_requested && !matches!(step.run_if.as_str(), "always" | "any_failed") {
                let result = synthetic(
                    step,
                    status::BLOCKED,
                    false,
                    "blocked because the pipeline stopped after a failure",
                    "pipeline_stopped",
                );
                state.statuses.insert(id.clone(), status::BLOCKED.into());
                state.results.insert(id.clone(), result.clone());
                hard_failure |= step.required;
                let _ = emit_event(ctx, "node_blocked", Some(id), result.data.clone());
                changed = true;
                continue;
            }

            let missing: Vec<String> = step
                .requires
                .iter()
                .filter(|capability| !provided.contains(*capability))
                .cloned()
                .collect();
            if !missing.is_empty() {
                let possible: HashSet<String> = pipeline
                    .steps
                    .iter()
                    .filter(|candidate| {
                        selected.contains(&candidate.id)
                            && !terminal(
                                state
                                    .statuses
                                    .get(&candidate.id)
                                    .map(String::as_str)
                                    .unwrap_or(status::PENDING),
                            )
                    })
                    .flat_map(|candidate| candidate.provides.iter().cloned())
                    .collect();
                if missing.iter().any(|capability| possible.contains(capability)) {
                    continue;
                }
                let mut result = synthetic(
                    step,
                    status::BLOCKED,
                    false,
                    "required capabilities were not provided",
                    "missing_capabilities",
                );
                result.set_data("missing_capabilities", serde_json::json!(missing));
                state.statuses.insert(id.clone(), status::BLOCKED.into());
                state.results.insert(id.clone(), result.clone());
                hard_failure |= step.required;
                let _ = emit_event(ctx, "node_blocked", Some(id), result.data.clone());
                changed = true;
                continue;
            }

            if barrier_running || (step.barrier && !running.is_empty()) {
                continue;
            }

            let Some(task_permit) = coordinator.try_task_slot() else {
                blocked_on_task_slot = true;
                break;
            };
            let resource_generation = coordinator.resources.generation();
            let Some(resource_lease) = coordinator.resources.try_acquire(step) else {
                drop(task_permit);
                blocked_on_resource.get_or_insert(resource_generation);
                continue;
            };

            state.statuses.insert(id.clone(), status::RUNNING.into());
            if state.save_async(&ctx.state_path).await.is_err() {
                state.statuses.insert(id.clone(), status::READY.into());
                drop(resource_lease);
                drop(task_permit);
                persistence_failed = true;
                stop_requested = true;
                hard_failure = true;
                break;
            }
            running.insert(id.clone(), step.barrier);
            let _ = emit_event(
                ctx,
                "node_started",
                Some(id),
                serde_json::json!({
                    "needs": step.needs,
                    "run_if": step.run_if,
                    "exclusive_resources": step.exclusive_resources,
                    "shared_resources": step.shared_resources,
                    "requires": step.requires,
                    "provides": step.provides,
                    "barrier": step.barrier,
                    "scheduler": "tokio",
                }),
            );

            let execute = execute.clone();
            let step_owned = step.clone();
            tasks.spawn(async move {
                // Keep the outer task alive even if an executor panics, so the
                // scheduler retains the exact step id and can report it.
                let nested_step = step_owned.clone();
                let future = execute(nested_step);
                let result = match tokio::spawn(future).await {
                    Ok(result) => result,
                    Err(_) => error_result(
                        &step_owned,
                        "unexpected executor panic",
                        "UNEXPECTED_EXECUTOR_ERROR",
                        "Keep the run report and add a regression test for the executor.",
                    ),
                };
                drop(resource_lease);
                drop(task_permit);
                (step_owned, result)
            });
            changed = true;
            if step.barrier {
                break;
            }
        }

        if !tasks.is_empty() {
            let mut global_progress = false;
            let joined = if ctx.cancellation.is_cancelled() {
                tasks.join_next().await
            } else if (blocked_on_resource.is_some() || blocked_on_task_slot)
                && running.len() < ctx.max_workers
            {
                tokio::select! {
                    _ = ctx.cancellation.cancelled() => None,
                    _ = coordinator.wait_for_progress(blocked_on_resource, blocked_on_task_slot) => {
                        global_progress = true;
                        None
                    },
                    joined = tasks.join_next() => joined,
                }
            } else {
                tokio::select! {
                    _ = ctx.cancellation.cancelled() => None,
                    joined = tasks.join_next() => joined,
                }
            };

            if global_progress || (joined.is_none() && ctx.cancellation.is_cancelled()) {
                continue;
            }

            match joined {
                Some(Ok((step_owned, mut result))) => {
                    let id = step_owned.id.clone();
                    running.remove(&id);
                    if !status::is_valid(&result.status) {
                        result.set_data(
                            "warning",
                            serde_json::json!(format!(
                                "executor returned unknown status '{}'",
                                result.status
                            )),
                        );
                    }
                    if result.success {
                        provided.extend(step_owned.provides.iter().cloned());
                    }
                    let failed = !result.success;
                    let policy = policy_for(&step_owned, &result);
                    let final_status = if result.success && result.status == status::OK {
                        status::SUCCEEDED.to_string()
                    } else {
                        result.status.clone()
                    };
                    state.statuses.insert(id.clone(), final_status);
                    state.results.insert(id.clone(), result.clone());
                    if state.save_async(&ctx.state_path).await.is_err() {
                        persistence_failed = true;
                        stop_requested = true;
                        hard_failure = true;
                    }
                    let _ = emit_event(
                        ctx,
                        "node_finished",
                        Some(&id),
                        serde_json::json!({
                            "success": result.success,
                            "status": result.status,
                            "message": result.message,
                            "policy": policy,
                        }),
                    );
                    if failed && step_owned.required {
                        hard_failure = true;
                        if policy == "stop" {
                            stop_requested = true;
                        }
                    }
                }
                Some(Err(_join_error)) => {
                    hard_failure = true;
                    stop_requested = true;
                    persistence_failed = true;
                }
                None => {}
            }
            continue;
        }

        let unresolved: Vec<String> = selected_order
            .iter()
            .filter(|id| {
                matches!(
                    state.statuses.get(*id).map(String::as_str),
                    Some(status::PENDING | status::READY | status::RUNNING)
                )
            })
            .cloned()
            .collect();
        if unresolved.is_empty() {
            break;
        }

        // A ready node may be waiting on another pipeline's global resource or
        // task budget. That is not a scheduler stall: await a global change.
        if blocked_on_resource.is_some() || blocked_on_task_slot {
            coordinator
                .wait_for_progress(blocked_on_resource, blocked_on_task_slot)
                .await;
            continue;
        }

        if !changed {
            for id in unresolved {
                let step = by_id[id.as_str()];
                let result = synthetic(
                    step,
                    status::BLOCKED,
                    false,
                    "scheduler could not find a valid transition",
                    "scheduler_stalled",
                );
                state.statuses.insert(id.clone(), status::BLOCKED.into());
                state.results.insert(id.clone(), result.clone());
                hard_failure |= step.required;
                let _ = emit_event(ctx, "node_blocked", Some(&id), result.data.clone());
            }
            let _ = state.save_async(&ctx.state_path).await;
            break;
        }
    }

    let mut results: Vec<StepResult> = selected_order
        .iter()
        .filter_map(|id| state.results.get(id).cloned())
        .collect();
    if persistence_failed {
        results.push(
            StepResult::new(
                "__state__",
                "state",
                false,
                status::FAILED,
                "run state could not be persisted; further scheduling was stopped",
            )
            .with_data(serde_json::json!({"error_code":"RUN_STATE_WRITE_ERROR"})),
        );
    }
    Ok(ScheduleOutput {
        results,
        success: !hard_failure && !persistence_failed,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::time::Duration;

    use pipecraft_core::load_pipeline;
    use crate::executor::{ExecutorRegistry, StepExecutor};
    use crate::PipelineEngine;

    struct ProbeExecutor {
        active: Arc<AtomicUsize>,
        max_active: Arc<AtomicUsize>,
        calls: Arc<AtomicUsize>,
    }

    impl StepExecutor for ProbeExecutor {
        fn step_type(&self) -> &'static str {
            "probe"
        }

        fn run(
            &self,
            step: &PipelineStep,
            _pipeline: &PipelineDefinition,
            _ctx: &ExecutionContext,
        ) -> StepResult {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let active = self.active.fetch_add(1, Ordering::SeqCst) + 1;
            self.max_active.fetch_max(active, Ordering::SeqCst);
            let delay = step.with.get("sleep_ms").and_then(|value| value.as_u64()).unwrap_or(20);
            std::thread::sleep(Duration::from_millis(delay));
            self.active.fetch_sub(1, Ordering::SeqCst);
            if step.with.get("fail").and_then(|value| value.as_bool()).unwrap_or(false) {
                StepResult::new(&step.id, &step.step_type, false, status::FAILED, "probe failure")
            } else {
                StepResult::new(&step.id, &step.step_type, true, status::OK, "probe ok")
            }
        }
    }

    fn engine() -> (PipelineEngine, Arc<AtomicUsize>, Arc<AtomicUsize>) {
        let active = Arc::new(AtomicUsize::new(0));
        let max_active = Arc::new(AtomicUsize::new(0));
        let calls = Arc::new(AtomicUsize::new(0));
        let mut registry = ExecutorRegistry::empty();
        registry.register(Box::new(ProbeExecutor {
            active: active.clone(),
            max_active: max_active.clone(),
            calls: calls.clone(),
        }));
        (PipelineEngine::new(registry), max_active, calls)
    }

    fn pipeline(dir: &std::path::Path, body: &str) -> PipelineDefinition {
        let path = dir.join("pipeline.yaml");
        std::fs::write(&path, body).unwrap();
        load_pipeline(&path).unwrap()
    }

    #[test]
    fn independent_nodes_can_run_concurrently() {
        let dir = tempfile::tempdir().unwrap();
        let definition = pipeline(
            dir.path(),
            r#"
schema_version: pipecraft/v1
name: concurrent
steps:
  - id: a
    type: probe
    with: {sleep_ms: 80}
  - id: b
    type: probe
    with: {sleep_ms: 80}
"#,
        );
        let (engine, max_active, _) = engine();
        let context = ExecutionContext::new(dir.path().to_path_buf())
            .dry_run(false)
            .max_workers(2);
        let run = engine.run(&definition, &context);
        assert!(run.success);
        assert!(max_active.load(Ordering::SeqCst) >= 2);
    }

    #[test]
    fn cancellation_stops_new_nodes() {
        let dir = tempfile::tempdir().unwrap();
        let definition = pipeline(
            dir.path(),
            r#"
schema_version: pipecraft/v1
name: cancellation
steps:
  - id: slow
    type: probe
    with: {sleep_ms: 80}
  - id: later
    type: probe
"#,
        );
        let (engine, _, calls) = engine();
        let token = crate::CancellationToken::new();
        let cancel = token.clone();
        let context = ExecutionContext::new(dir.path().to_path_buf())
            .dry_run(false)
            .max_workers(1)
            .cancellation_token(token);
        let handle = std::thread::spawn(move || engine.run(&definition, &context));
        std::thread::sleep(Duration::from_millis(20));
        cancel.cancel();
        let run = handle.join().unwrap();
        assert!(!run.success);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert_eq!(
            run.results.iter().find(|result| result.step_id == "later").unwrap().status,
            status::CANCELLED
        );
    }
}
