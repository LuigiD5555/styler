//! `pipecraft-report` — structured results, run reports and the JSON writer.
//!
//! V1.1 writes each run into its own folder:
//! `.pipelines/runs/<run-id>/report.json`, with sibling `logs/` and
//! `artifacts/` folders populated by executors.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// Canonical step statuses. Stored as plain strings for forward-compatibility
/// (executors may report a status the runtime then flags as unknown), with
/// constants so call sites don't stringly-type by hand.
pub mod status {
    pub const OK: &str = "ok";
    pub const OK_WITH_WARNINGS: &str = "ok_with_warnings";
    pub const SKIPPED: &str = "skipped";
    pub const FAILED: &str = "failed";
    pub const NEEDS_APPROVAL: &str = "needs_approval";
    pub const DRY_RUN: &str = "dry_run";
    pub const PLANNED: &str = "planned";
    pub const TIMEOUT: &str = "timeout";
    pub const CANCELLED: &str = "cancelled";
    pub const PENDING: &str = "pending";
    pub const READY: &str = "ready";
    pub const RUNNING: &str = "running";
    pub const SUCCEEDED: &str = "succeeded";
    pub const BLOCKED: &str = "blocked";

    pub const ALL: [&str; 14] = [
        OK, OK_WITH_WARNINGS, SKIPPED, FAILED, NEEDS_APPROVAL, DRY_RUN, PLANNED, TIMEOUT,
        CANCELLED, PENDING, READY, RUNNING, SUCCEEDED, BLOCKED,
    ];

    pub fn is_valid(s: &str) -> bool {
        ALL.contains(&s)
    }
}

/// Result produced by one step executor.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StepResult {
    pub step_id: String,
    #[serde(rename = "type")]
    pub step_type: String,
    pub success: bool,
    pub status: String,
    pub message: String,
    pub output: String,
    pub data: serde_json::Value,
}

impl StepResult {
    pub fn new(
        step_id: impl Into<String>,
        step_type: impl Into<String>,
        success: bool,
        status: impl Into<String>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            step_id: step_id.into(),
            step_type: step_type.into(),
            success,
            status: status.into(),
            message: message.into(),
            output: String::new(),
            data: serde_json::Value::Object(Default::default()),
        }
    }

    pub fn with_output(mut self, output: impl Into<String>) -> Self {
        self.output = output.into();
        self
    }

    pub fn with_data(mut self, data: serde_json::Value) -> Self {
        self.data = data;
        self
    }

    /// Insert a key into the (object) `data` map, creating it if needed.
    pub fn set_data(&mut self, key: &str, value: serde_json::Value) {
        if !self.data.is_object() {
            self.data = serde_json::Value::Object(Default::default());
        }
        if let Some(map) = self.data.as_object_mut() {
            map.insert(key.to_string(), value);
        }
    }
}

/// Complete execution report for one pipeline run.
#[derive(Debug, Clone, Serialize)]
pub struct PipelineRun {
    pub run_id: String,
    pub pipeline: String,
    pub success: bool,
    pub dry_run: bool,
    pub labels: Vec<String>,
    pub started_at: String,
    pub finished_at: String,
    pub results: Vec<StepResult>,
    /// Topological order the runtime computed (informational / auditable).
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub order: Vec<String>,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub selected_from: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub selected_only: Vec<String>,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub run_dir: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub artifacts_dir: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub logs_dir: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub events_path: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub state_path: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub plan_fingerprint: String,
    pub max_workers: usize,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub runtime: String,
    pub global_max_tasks: usize,
    #[serde(skip)]
    pub report_path: String,
}

/// Persist a JSON audit report under `runs_dir/<run-id>/report.json`, returning
/// the written path.
pub fn write_run(run: &mut PipelineRun, root: &Path, runs_dir: &str) -> std::io::Result<PathBuf> {
    let out = root.join(runs_dir).join(&run.run_id);
    let artifacts = out.join("artifacts");
    let logs = out.join("logs");
    std::fs::create_dir_all(&artifacts)?;
    std::fs::create_dir_all(&logs)?;
    let path = out.join("report.json");

    run.run_dir = out.display().to_string();
    run.artifacts_dir = artifacts.display().to_string();
    run.logs_dir = logs.display().to_string();

    // Build the on-disk object explicitly so the schema stays stable regardless
    // of struct field additions.
    let mut obj: BTreeMap<&str, serde_json::Value> = BTreeMap::new();
    obj.insert("run_id", serde_json::json!(run.run_id));
    obj.insert("pipeline", serde_json::json!(run.pipeline));
    obj.insert("success", serde_json::json!(run.success));
    obj.insert("dry_run", serde_json::json!(run.dry_run));
    obj.insert("labels", serde_json::json!(run.labels));
    obj.insert("started_at", serde_json::json!(run.started_at));
    obj.insert("finished_at", serde_json::json!(run.finished_at));
    obj.insert("order", serde_json::json!(run.order));
    obj.insert("selected_from", serde_json::json!(run.selected_from));
    obj.insert("selected_only", serde_json::json!(run.selected_only));
    obj.insert("run_dir", serde_json::json!(run.run_dir));
    obj.insert("artifacts_dir", serde_json::json!(run.artifacts_dir));
    obj.insert("logs_dir", serde_json::json!(run.logs_dir));
    obj.insert("events_path", serde_json::json!(run.events_path));
    obj.insert("state_path", serde_json::json!(run.state_path));
    obj.insert("plan_fingerprint", serde_json::json!(run.plan_fingerprint));
    obj.insert("max_workers", serde_json::json!(run.max_workers));
    obj.insert("runtime", serde_json::json!(run.runtime));
    obj.insert("global_max_tasks", serde_json::json!(run.global_max_tasks));
    obj.insert("results", serde_json::to_value(&run.results).unwrap_or_default());

    use std::io::Write;
    let json = serde_json::to_vec_pretty(&obj).unwrap_or_else(|_| b"{}".to_vec());
    let tmp = out.join(format!("report.json.tmp-{}", std::process::id()));
    let mut file = std::fs::File::create(&tmp)?;
    file.write_all(&json)?;
    file.sync_all()?;
    drop(file);
    if let Err(first) = std::fs::rename(&tmp, &path) {
        if path.exists() {
            std::fs::remove_file(&path)?;
            std::fs::rename(&tmp, &path)?;
        } else {
            return Err(first);
        }
    }
    if let Ok(dir) = std::fs::File::open(&out) { let _ = dir.sync_all(); }
    run.report_path = path.display().to_string();
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_report_with_pipeline_name() {
        let dir = tempfile::tempdir().unwrap();
        let mut run = PipelineRun {
            run_id: "20260101T000000Z-demo-abcd".into(),
            pipeline: "demo".into(),
            success: true,
            dry_run: true,
            labels: vec!["demo".into()],
            started_at: "t0".into(),
            finished_at: "t1".into(),
            results: vec![StepResult::new("a", "note", true, status::OK, "ok")],
            order: vec!["a".into()],
            selected_from: String::new(),
            selected_only: Vec::new(),
            run_dir: String::new(),
            artifacts_dir: String::new(),
            logs_dir: String::new(),
            events_path: String::new(),
            state_path: String::new(),
            plan_fingerprint: String::new(),
            max_workers: 1,
            runtime: "rust-tokio".into(),
            global_max_tasks: 1,
            report_path: String::new(),
        };
        let path = write_run(&mut run, dir.path(), ".pipelines/runs").unwrap();
        assert!(path.exists());
        assert!(path.ends_with("report.json"));
        assert!(dir.path().join(".pipelines/runs/20260101T000000Z-demo-abcd/logs").exists());
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(text.contains("\"pipeline\": \"demo\""));
    }
}
