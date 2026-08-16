use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use chrono::Utc;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobRequest {
    pub pipeline: String,
    pub execute: bool,
    pub approve: bool,
    pub labels: Vec<String>,
    pub max_workers: usize,
    pub from_step: Option<String>,
    pub only: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum JobStatus {
    Queued,
    Running,
    Succeeded,
    Failed,
    Cancelled,
    Interrupted,
}

impl JobStatus {
    pub fn terminal(&self) -> bool {
        matches!(self, Self::Succeeded | Self::Failed | Self::Cancelled | Self::Interrupted)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobRecord {
    pub protocol: String,
    pub run_id: String,
    pub status: JobStatus,
    pub request: JobRequest,
    #[serde(default = "default_runs_dir")]
    pub runs_dir: String,
    #[serde(default)]
    pub pipeline_snapshot_path: String,
    #[serde(default)]
    pub runtime_version: String,
    pub submitted_at: String,
    pub started_at: Option<String>,
    pub finished_at: Option<String>,
    pub updated_at: String,
    pub message: String,
    pub attempts: u32,
    pub resume_requested: bool,
    pub report_path: Option<String>,
    #[serde(default)]
    pub attempt_reports: Vec<String>,
    pub events_path: String,
    pub state_path: String,
    pub warning: Option<String>,
}

fn default_runs_dir() -> String { ".pipelines/runs".into() }

impl JobRecord {
    pub fn new(
        run_id: String,
        request: JobRequest,
        runs_dir: &str,
        root: &Path,
        pipeline_snapshot_path: String,
    ) -> Self {
        let now = Utc::now().to_rfc3339();
        let run_dir = root.join(runs_dir).join(&run_id);
        Self {
            protocol: "pipecraft.job/v1".into(),
            run_id,
            status: JobStatus::Queued,
            request,
            runs_dir: runs_dir.into(),
            pipeline_snapshot_path,
            runtime_version: env!("CARGO_PKG_VERSION").into(),
            submitted_at: now.clone(),
            started_at: None,
            finished_at: None,
            updated_at: now,
            message: "queued".into(),
            attempts: 0,
            resume_requested: false,
            report_path: None,
            attempt_reports: Vec::new(),
            events_path: run_dir.join("events.jsonl").display().to_string(),
            state_path: run_dir.join("state.json").display().to_string(),
            warning: None,
        }
    }

    pub fn touch(&mut self) { self.updated_at = Utc::now().to_rfc3339(); }
}

#[derive(Debug, Clone)]
pub struct JobStore {
    dir: PathBuf,
}

impl JobStore {
    pub fn new(dir: PathBuf) -> Self { Self { dir } }
    pub fn dir(&self) -> &Path { &self.dir }

    pub async fn prepare(&self) -> std::io::Result<()> {
        tokio::fs::create_dir_all(&self.dir).await
    }

    pub fn path(&self, run_id: &str) -> PathBuf { self.dir.join(format!("{run_id}.json")) }

    pub async fn save(&self, record: &JobRecord) -> std::io::Result<()> {
        self.prepare().await?;
        let path = self.path(&record.run_id);
        let tmp = path.with_extension(format!("json.tmp-{}", uuid::Uuid::new_v4().simple()));
        use tokio::io::AsyncWriteExt;
        let bytes = serde_json::to_vec_pretty(record)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
        let mut file = tokio::fs::File::create(&tmp).await?;
        file.write_all(&bytes).await?;
        file.flush().await?;
        file.sync_all().await?;
        drop(file);
        if let Err(first) = tokio::fs::rename(&tmp, &path).await {
            if tokio::fs::try_exists(&path).await.unwrap_or(false) {
                tokio::fs::remove_file(&path).await?;
                tokio::fs::rename(&tmp, &path).await?;
            } else {
                return Err(first);
            }
        }
        if let Some(parent) = path.parent() {
            if let Ok(dir) = std::fs::File::open(parent) { let _ = dir.sync_all(); }
        }
        Ok(())
    }

    pub async fn load(&self, run_id: &str) -> std::io::Result<JobRecord> {
        let bytes = tokio::fs::read(self.path(run_id)).await?;
        serde_json::from_slice(&bytes)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))
    }

    pub async fn load_all(&self) -> std::io::Result<BTreeMap<String, JobRecord>> {
        self.prepare().await?;
        let mut out = BTreeMap::new();
        let mut entries = tokio::fs::read_dir(&self.dir).await?;
        while let Some(entry) = entries.next_entry().await? {
            let path = entry.path();
            if path.extension().and_then(|value| value.to_str()) != Some("json") { continue; }
            let Ok(bytes) = tokio::fs::read(&path).await else { continue; };
            let Ok(record) = serde_json::from_slice::<JobRecord>(&bytes) else { continue; };
            out.insert(record.run_id.clone(), record);
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn job_record_round_trips_atomically() {
        let dir = tempfile::tempdir().unwrap();
        let store = JobStore::new(dir.path().join("jobs"));
        let request = JobRequest {
            pipeline: "demo".into(),
            execute: false,
            approve: false,
            labels: vec!["test".into()],
            max_workers: 2,
            from_step: None,
            only: vec![],
        };
        let record = JobRecord::new("run-1".into(), request, ".pipelines/runs", dir.path(), "snapshot.yaml".into());
        store.save(&record).await.unwrap();
        let loaded = store.load("run-1").await.unwrap();
        assert_eq!(loaded.run_id, "run-1");
        assert_eq!(loaded.status, JobStatus::Queued);
        assert_eq!(loaded.request.pipeline, "demo");
    }
}
