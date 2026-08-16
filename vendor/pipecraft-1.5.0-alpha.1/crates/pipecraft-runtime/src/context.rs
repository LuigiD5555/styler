//! Runtime settings for a single pipeline run.

use std::path::PathBuf;
use std::sync::{Arc, atomic::{AtomicBool, Ordering}};

use tokio::sync::Notify;

use pipecraft_core::yamlx::WithMap;


/// Cooperative cancellation shared by the scheduler and process executors.
/// Clones refer to the same atomic flag, so callers can keep one handle and
/// cancel a running engine from another thread.
#[derive(Debug, Clone, Default)]
pub struct CancellationToken {
    cancelled: Arc<AtomicBool>,
    notify: Arc<Notify>,
}

impl CancellationToken {
    pub fn new() -> Self { Self::default() }
    pub fn cancel(&self) {
        if !self.cancelled.swap(true, Ordering::SeqCst) {
            self.notify.notify_waiters();
        }
    }
    pub fn is_cancelled(&self) -> bool { self.cancelled.load(Ordering::SeqCst) }
    pub async fn cancelled(&self) {
        if self.is_cancelled() {
            return;
        }
        let notified = self.notify.notified();
        if self.is_cancelled() {
            return;
        }
        notified.await;
    }
}

/// Everything an executor needs to know about the run it is part of.
#[derive(Debug, Clone)]
pub struct ExecutionContext {
    pub root: PathBuf,
    pub labels: Vec<String>,
    pub run_id: String,
    /// Plan-only / no-side-effects mode. Defaults to true everywhere; only
    /// `--execute` flips it off.
    pub dry_run: bool,
    /// Whether gated steps (`requires_approval`, `manual_approval`, apply-mode
    /// sync) are approved for this run.
    pub approve: bool,
    pub route: String,
    pub context: WithMap,
    /// Workspace-configurable directory that contains one folder per run.
    pub runs_dir: String,
    /// Filled by the engine once the final run id is known.
    pub run_dir: PathBuf,
    pub artifacts_dir: PathBuf,
    pub logs_dir: PathBuf,
    /// Structured runtime events and resumable state for this run.
    pub events_path: PathBuf,
    pub state_path: PathBuf,
    /// Maximum number of concurrently running nodes. Defaults to 1 for V1
    /// compatibility; callers opt in to parallel scheduling explicitly.
    pub max_workers: usize,
    /// Reuse terminal successful nodes from state.json when the plan fingerprint
    /// matches.
    pub resume: bool,
    pub cancellation: CancellationToken,
    /// Run only steps at or after this id in the resolved topological order.
    pub from_step: Option<String>,
    /// Run only these step ids. If combined with `from_step`, this filters the
    /// suffix that starts at `from_step`.
    pub only_steps: Vec<String>,
}

impl ExecutionContext {
    pub fn new(root: PathBuf) -> Self {
        Self {
            root,
            labels: Vec::new(),
            run_id: String::new(),
            dry_run: true,
            approve: false,
            route: String::new(),
            context: WithMap::new(),
            runs_dir: ".pipelines/runs".into(),
            run_dir: PathBuf::new(),
            artifacts_dir: PathBuf::new(),
            logs_dir: PathBuf::new(),
            events_path: PathBuf::new(),
            state_path: PathBuf::new(),
            max_workers: 1,
            resume: false,
            cancellation: CancellationToken::new(),
            from_step: None,
            only_steps: Vec::new(),
        }
    }

    pub fn labels(mut self, labels: Vec<String>) -> Self {
        self.labels = labels;
        self
    }
    pub fn run_id(mut self, run_id: impl Into<String>) -> Self {
        self.run_id = run_id.into();
        self
    }
    pub fn dry_run(mut self, dry_run: bool) -> Self {
        self.dry_run = dry_run;
        self
    }
    pub fn approve(mut self, approve: bool) -> Self {
        self.approve = approve;
        self
    }
    pub fn context(mut self, context: WithMap) -> Self {
        self.context = context;
        self
    }
    pub fn runs_dir(mut self, runs_dir: impl Into<String>) -> Self {
        self.runs_dir = runs_dir.into();
        self
    }
    pub fn from_step(mut self, from_step: Option<String>) -> Self {
        self.from_step = from_step;
        self
    }
    pub fn only_steps(mut self, only_steps: Vec<String>) -> Self {
        self.only_steps = only_steps;
        self
    }
    pub fn max_workers(mut self, max_workers: usize) -> Self {
        self.max_workers = max_workers.max(1);
        self
    }
    pub fn resume(mut self, resume: bool) -> Self {
        self.resume = resume;
        self
    }
    pub fn cancellation_token(mut self, token: CancellationToken) -> Self {
        self.cancellation = token;
        self
    }

    /// Attach the computed run id and derive run/artifact/log directories.
    pub fn for_run(mut self, run_id: String) -> Self {
        self.run_id = run_id.clone();
        self.run_dir = self.root.join(&self.runs_dir).join(&run_id);
        self.artifacts_dir = self.run_dir.join("artifacts");
        self.logs_dir = self.run_dir.join("logs");
        self.events_path = self.run_dir.join("events.jsonl");
        self.state_path = self.run_dir.join("state.json");
        self
    }
}
