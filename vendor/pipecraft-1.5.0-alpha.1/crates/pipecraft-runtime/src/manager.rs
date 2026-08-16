//! Multi-pipeline runtime manager.
//!
//! A manager owns one Tokio runtime worth of scheduling policy: a global task
//! budget, a global resource table, and a pipeline-level concurrency limit.
//! Individual pipelines still enforce their own `ExecutionContext.max_workers`.

use std::sync::Arc;

use pipecraft_core::model::PipelineDefinition;
use pipecraft_report::PipelineRun;
use tokio::sync::Semaphore;
use tokio::task::JoinSet;

use crate::{ExecutionContext, PipelineEngine, RuntimeCoordinator};
use crate::engine::panic_run;

#[derive(Debug, Clone)]
pub struct RuntimeLimits {
    pub worker_threads: usize,
    pub max_pipelines: usize,
    pub max_tasks: usize,
}

impl Default for RuntimeLimits {
    fn default() -> Self {
        let cores = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4);
        Self {
            worker_threads: cores.max(2),
            max_pipelines: 8,
            max_tasks: (cores * 4).max(8),
        }
    }
}

#[derive(Clone)]
pub struct PipelineRequest {
    pub pipeline: Arc<PipelineDefinition>,
    pub context: ExecutionContext,
}

impl PipelineRequest {
    pub fn new(pipeline: PipelineDefinition, context: ExecutionContext) -> Self {
        Self { pipeline: Arc::new(pipeline), context }
    }
}

#[derive(Clone)]
pub struct RuntimeManager {
    engine: Arc<PipelineEngine>,
    limits: RuntimeLimits,
    coordinator: RuntimeCoordinator,
    pipeline_slots: Arc<Semaphore>,
}

impl RuntimeManager {
    pub fn new(engine: PipelineEngine, limits: RuntimeLimits) -> Self {
        let max_pipelines = limits.max_pipelines.max(1);
        let coordinator = RuntimeCoordinator::new(limits.max_tasks);
        Self {
            engine: Arc::new(engine),
            limits,
            coordinator,
            pipeline_slots: Arc::new(Semaphore::new(max_pipelines)),
        }
    }

    pub fn limits(&self) -> &RuntimeLimits {
        &self.limits
    }

    pub async fn run_many_async(&self, requests: Vec<PipelineRequest>) -> Vec<PipelineRun> {
        let total = requests.len();
        let mut set = JoinSet::new();
        for (index, request) in requests.into_iter().enumerate() {
            let engine = self.engine.clone();
            let coordinator = self.coordinator.clone();
            let slots = self.pipeline_slots.clone();
            set.spawn(async move {
                let pipeline = request.pipeline.clone();
                let context = request.context.clone();
                let _pipeline_permit = match slots.acquire_owned().await {
                    Ok(permit) => permit,
                    Err(_) => {
                        return (index, panic_run(&pipeline, &context, "pipeline semaphore closed"));
                    }
                };
                // Keep this outer task alive so a bug in one pipeline cannot make
                // its result silently disappear from a multi-pipeline batch.
                let nested_pipeline = pipeline.clone();
                let nested_context = context.clone();
                let run = match tokio::spawn(async move {
                    engine
                        .run_async_with_coordinator(&nested_pipeline, &nested_context, coordinator)
                        .await
                })
                .await
                {
                    Ok(run) => run,
                    Err(_) => panic_run(&pipeline, &context, "pipeline runtime task panicked"),
                };
                (index, run)
            });
        }

        let mut ordered: Vec<Option<PipelineRun>> = (0..total).map(|_| None).collect();
        while let Some(joined) = set.join_next().await {
            if let Ok((index, run)) = joined {
                ordered[index] = Some(run);
            }
        }
        ordered.into_iter().flatten().collect()
    }

    pub fn run_many(&self, requests: Vec<PipelineRequest>) -> Vec<PipelineRun> {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(self.limits.worker_threads.max(1))
            .enable_all()
            .build()
            .expect("failed to build PipeCraft Tokio runtime");
        runtime.block_on(self.run_many_async(requests))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::time::Duration;

    use pipecraft_core::{load_pipeline, model::{PipelineDefinition, PipelineStep}};
    use pipecraft_report::{status, StepResult};

    use crate::executor::{ExecutorRegistry, StepExecutor};

    struct ProbeExecutor {
        active: Arc<AtomicUsize>,
        max_active: Arc<AtomicUsize>,
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
            let active = self.active.fetch_add(1, Ordering::SeqCst) + 1;
            self.max_active.fetch_max(active, Ordering::SeqCst);
            std::thread::sleep(Duration::from_millis(60));
            self.active.fetch_sub(1, Ordering::SeqCst);
            StepResult::new(&step.id, &step.step_type, true, status::OK, "ok")
        }
    }

    fn definition(dir: &std::path::Path, name: &str, resource: bool) -> PipelineDefinition {
        let resource_line = if resource {
            "    exclusive_resources: [global-db]\n"
        } else {
            ""
        };
        let text = format!(
            "schema_version: pipecraft/v1\nname: {name}\nsteps:\n  - id: work\n    type: probe\n{resource_line}"
        );
        let path = dir.join(format!("{name}.yaml"));
        std::fs::write(&path, text).unwrap();
        load_pipeline(&path).unwrap()
    }

    fn manager(max_active: Arc<AtomicUsize>) -> RuntimeManager {
        let active = Arc::new(AtomicUsize::new(0));
        let mut registry = ExecutorRegistry::empty();
        registry.register(Box::new(ProbeExecutor { active, max_active }));
        RuntimeManager::new(
            PipelineEngine::new(registry),
            RuntimeLimits {
                worker_threads: 2,
                max_pipelines: 2,
                max_tasks: 2,
            },
        )
    }

    #[test]
    fn global_exclusive_resource_serializes_different_pipelines() {
        let dir = tempfile::tempdir().unwrap();
        let max_active = Arc::new(AtomicUsize::new(0));
        let manager = manager(max_active.clone());
        let context = || {
            ExecutionContext::new(dir.path().to_path_buf())
                .dry_run(false)
                .max_workers(1)
        };
        let runs = manager.run_many(vec![
            PipelineRequest::new(definition(dir.path(), "a", true), context()),
            PipelineRequest::new(definition(dir.path(), "b", true), context()),
        ]);
        assert_eq!(runs.len(), 2);
        assert!(runs.iter().all(|run| run.success));
        assert_eq!(max_active.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn independent_pipelines_share_the_rust_runtime_concurrently() {
        let dir = tempfile::tempdir().unwrap();
        let max_active = Arc::new(AtomicUsize::new(0));
        let manager = manager(max_active.clone());
        let context = || {
            ExecutionContext::new(dir.path().to_path_buf())
                .dry_run(false)
                .max_workers(1)
        };
        let runs = manager.run_many(vec![
            PipelineRequest::new(definition(dir.path(), "a", false), context()),
            PipelineRequest::new(definition(dir.path(), "b", false), context()),
        ]);
        assert!(runs.iter().all(|run| run.success));
        assert!(max_active.load(Ordering::SeqCst) >= 2);
    }
}
