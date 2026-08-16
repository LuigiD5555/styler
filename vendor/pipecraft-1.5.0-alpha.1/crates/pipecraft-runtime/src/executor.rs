//! The `StepExecutor` strategy interface, a registry of built-ins, the repo
//! resolver, and a small helper for building failed `StepResult`s with codes.

use std::collections::{BTreeMap, HashSet};
use std::future::Future;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::Arc;

use pipecraft_core::error::ConfigError;
use pipecraft_core::model::{PipelineDefinition, PipelineStep};
use pipecraft_core::yamlx;
use pipecraft_report::{status, StepResult};

use crate::context::ExecutionContext;

pub type StepFuture<'a> = Pin<Box<dyn Future<Output = StepResult> + Send + 'a>>;

/// Strategy interface for one step type. The synchronous `run` method remains
/// for source compatibility. The Rust runtime itself calls `run_async`.
/// Built-in process executors override it with Tokio I/O; legacy/custom sync
/// executors are isolated with `block_in_place` so unrelated tasks keep moving.
pub trait StepExecutor: Send + Sync {
    fn step_type(&self) -> &'static str;
    fn run(&self, step: &PipelineStep, pipeline: &PipelineDefinition, ctx: &ExecutionContext) -> StepResult;

    fn run_async<'a>(
        &'a self,
        step: &'a PipelineStep,
        pipeline: &'a PipelineDefinition,
        ctx: &'a ExecutionContext,
    ) -> StepFuture<'a> {
        Box::pin(async move {
            tokio::task::block_in_place(|| self.run(step, pipeline, ctx))
        })
    }
}

/// Build a `failed` step result carrying an error code and optional hint.
pub fn error_result(
    step: &PipelineStep,
    message: impl Into<String>,
    code: &str,
    hint: &str,
) -> StepResult {
    let mut data = serde_json::Map::new();
    data.insert("error_code".into(), serde_json::json!(code));
    if !hint.is_empty() {
        data.insert("hint".into(), serde_json::json!(hint));
    }
    StepResult::new(step.id.clone(), step.step_type.clone(), false, status::FAILED, message)
        .with_data(serde_json::Value::Object(data))
}

/// Map a `ConfigError` (e.g. from repo resolution) onto a failed step result.
pub fn error_result_from_config(step: &PipelineStep, err: &ConfigError) -> StepResult {
    error_result(step, err.message.clone(), &err.code, &err.hint)
}

/// Resolve YAML repo ids to absolute local paths.
pub struct RepositoryResolver<'a> {
    root: PathBuf,
    pipeline: &'a PipelineDefinition,
}

impl<'a> RepositoryResolver<'a> {
    pub fn new(root: &Path, pipeline: &'a PipelineDefinition) -> Self {
        Self { root: root.to_path_buf(), pipeline }
    }

    pub fn resolve(&self, repo_id: &str) -> Result<PathBuf, ConfigError> {
        let root = self.root.canonicalize().unwrap_or_else(|_| self.root.clone());
        if repo_id.is_empty() {
            return Ok(root);
        }
        let repo = self.pipeline.repos.get(repo_id).ok_or_else(|| {
            ConfigError::new(
                format!("Unknown repo '{repo_id}' in pipeline '{}'", self.pipeline.name),
                "UNKNOWN_REPO",
            )
            .with_hint(format!(
                "Add `repos: {repo_id}: {{path: ...}}` or correct the step repo name."
            ))
        })?;
        if !repo.is_mapping() {
            return Err(ConfigError::new(
                format!("Repo '{repo_id}' must be a mapping in pipeline '{}'", self.pipeline.name),
                "REPO_CONFIG_ERROR",
            )
            .with_hint("Use `repos: my_repo: {path: ../my-repo}`."));
        }
        let path = yamlx::get(repo, "path").and_then(yamlx::as_string).unwrap_or_else(|| ".".into());
        let p = PathBuf::from(&path);
        if p.is_absolute() {
            Ok(p)
        } else {
            Ok(root.join(p))
        }
    }
}

/// Registry of step type -> executor.
#[derive(Clone)]
pub struct ExecutorRegistry {
    executors: BTreeMap<String, Arc<dyn StepExecutor>>,
}

impl ExecutorRegistry {
    pub fn empty() -> Self {
        Self { executors: BTreeMap::new() }
    }

    pub fn register(&mut self, executor: Box<dyn StepExecutor>) {
        let executor: Arc<dyn StepExecutor> = Arc::from(executor);
        self.executors.insert(executor.step_type().to_string(), executor);
    }

    pub fn register_arc(&mut self, executor: Arc<dyn StepExecutor>) {
        self.executors.insert(executor.step_type().to_string(), executor);
    }

    pub fn get(&self, step_type: &str) -> Option<Arc<dyn StepExecutor>> {
        self.executors.get(step_type).cloned()
    }

    pub fn known_types(&self) -> HashSet<String> {
        self.executors.keys().cloned().collect()
    }

    /// The default V1.1 registry: note, checklist, command, plugin, file_check,
    /// boundary_check, git_diff, copy_or_sync, manual_approval, target_plan.
    pub fn default_v1() -> Self {
        use crate::steps::*;
        let mut reg = Self::empty();
        reg.register(Box::new(NoteExecutor));
        reg.register(Box::new(ChecklistExecutor));
        reg.register(Box::new(ManualApprovalExecutor));
        reg.register(Box::new(CommandExecutor));
        reg.register(Box::new(PluginExecutor));
        reg.register(Box::new(FileCheckExecutor));
        reg.register(Box::new(BoundaryCheckExecutor));
        reg.register(Box::new(GitDiffExecutor));
        reg.register(Box::new(CopyOrSyncExecutor));
        reg.register(Box::new(TargetPlanExecutor));
        reg
    }
}

impl Default for ExecutorRegistry {
    fn default() -> Self {
        Self::default_v1()
    }
}
