use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use chrono::Utc;
use pipecraft_core::{load_pipeline, PipelineCatalog};
use pipecraft_report::write_run;
use pipecraft_runtime::{CancellationToken, ExecutionContext, PipelineEngine, RuntimeCoordinator};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::sync::{Mutex, RwLock, Semaphore};

use crate::lock::WorkspaceRuntimeLock;
use crate::protocol::{IpcRequest, IpcResponse, PROTOCOL_VERSION};
use crate::store::{JobRecord, JobRequest, JobStatus, JobStore};

const MAX_REQUEST_BYTES: usize = 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecoveryPolicy { Manual, Auto }

#[derive(Debug, Clone)]
pub struct ServiceConfig {
    pub worker_threads: usize,
    pub max_pipelines: usize,
    pub max_tasks: usize,
    pub recovery: RecoveryPolicy,
}

impl Default for ServiceConfig {
    fn default() -> Self {
        let cores = std::thread::available_parallelism().map(|value| value.get()).unwrap_or(4);
        Self {
            worker_threads: cores.max(2),
            max_pipelines: 8,
            max_tasks: (cores * 4).max(8),
            recovery: RecoveryPolicy::Manual,
        }
    }
}

#[derive(Clone)]
pub struct RuntimeService {
    root: PathBuf,
    runs_dir: String,
    engine: Arc<PipelineEngine>,
    coordinator: RuntimeCoordinator,
    pipeline_slots: Arc<Semaphore>,
    store: JobStore,
    jobs: Arc<RwLock<BTreeMap<String, JobRecord>>>,
    cancellations: Arc<Mutex<BTreeMap<String, CancellationToken>>>,
    config: ServiceConfig,
    _service_lock: Arc<WorkspaceRuntimeLock>,
}

impl RuntimeService {
    pub async fn open(root: PathBuf, config: ServiceConfig) -> Result<Self, String> {
        let catalog = PipelineCatalog::open(&root).map_err(|error| error.to_string())?;
        let runtime_dir = root.join(".pipelines/runtime");
        let service_lock = Arc::new(WorkspaceRuntimeLock::acquire(&runtime_dir)?);
        let jobs_dir = runtime_dir.join("jobs");
        let store = JobStore::new(jobs_dir);
        store.prepare().await.map_err(|error| error.to_string())?;
        let jobs = store.load_all().await.map_err(|error| error.to_string())?;
        let service = Self {
            root,
            runs_dir: catalog.workspace.runs_dir.clone(),
            engine: Arc::new(PipelineEngine::default()),
            coordinator: RuntimeCoordinator::new(config.max_tasks),
            pipeline_slots: Arc::new(Semaphore::new(config.max_pipelines.max(1))),
            store,
            jobs: Arc::new(RwLock::new(jobs)),
            cancellations: Arc::new(Mutex::new(BTreeMap::new())),
            config,
            _service_lock: service_lock,
        };
        service.recover_startup().await?;
        Ok(service)
    }

    pub fn root(&self) -> &Path { &self.root }

    async fn recover_startup(&self) -> Result<(), String> {
        let candidates = {
            let jobs = self.jobs.read().await;
            jobs.values()
                .filter(|record| matches!(record.status, JobStatus::Queued | JobStatus::Running))
                .map(|record| record.run_id.clone())
                .collect::<Vec<_>>()
        };

        for run_id in candidates {
            let mut record = self.get_job(&run_id).await.ok_or_else(|| "job disappeared during recovery".to_string())?;
            if let Some(recovered) = self.recover_completed_report(record.clone()).await? {
                self.put_job(recovered).await?;
                continue;
            }
            record.status = JobStatus::Interrupted;
            record.finished_at = Some(Utc::now().to_rfc3339());
            record.message = "runtime service restarted while this job was active".into();
            record.warning = Some("the interrupted node may have produced partial external effects; successful persisted nodes will not be replayed".into());
            record.touch();
            self.put_job(record.clone()).await?;
            if self.config.recovery == RecoveryPolicy::Auto {
                self.start_existing(record.run_id.clone(), true).await?;
            }
        }
        Ok(())
    }

    async fn recover_completed_report(&self, mut record: JobRecord) -> Result<Option<JobRecord>, String> {
        let path = self.root.join(&record.runs_dir).join(&record.run_id).join("report.json");
        let bytes = match tokio::fs::read(&path).await {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.to_string()),
        };
        let value: Value = serde_json::from_slice(&bytes).map_err(|error| error.to_string())?;
        if value.get("run_id").and_then(Value::as_str) != Some(record.run_id.as_str())
            || value.get("pipeline").and_then(Value::as_str) != Some(record.request.pipeline.as_str())
        {
            return Ok(None);
        }
        let cancelled = value.get("results")
            .and_then(Value::as_array)
            .map(|results| results.iter().any(|result| result.get("status").and_then(Value::as_str) == Some("cancelled")))
            .unwrap_or(false);
        record.status = if cancelled {
            JobStatus::Cancelled
        } else if value.get("success").and_then(Value::as_bool).unwrap_or(false) {
            JobStatus::Succeeded
        } else {
            JobStatus::Failed
        };
        record.report_path = Some(path.display().to_string());
        let attempt_dir = self.root.join(&record.runs_dir).join(&record.run_id).join("attempts");
        let attempt_path = attempt_dir.join(format!("attempt-{:03}.json", record.attempts.max(1)));
        if !record.attempt_reports.iter().any(|item| item == &attempt_path.display().to_string()) {
            let source = path.clone();
            let destination = attempt_path.clone();
            tokio::task::spawn_blocking(move || durable_copy(&source, &destination))
                .await
                .map_err(|error| error.to_string())?
                .map_err(|error| error.to_string())?;
            record.attempt_reports.push(attempt_path.display().to_string());
        }
        record.finished_at = value.get("finished_at").and_then(Value::as_str).map(str::to_string)
            .or_else(|| Some(Utc::now().to_rfc3339()));
        record.message = "recovered durable final report after runtime restart".into();
        record.warning = None;
        record.touch();
        Ok(Some(record))
    }

    async fn get_job(&self, run_id: &str) -> Option<JobRecord> {
        self.jobs.read().await.get(run_id).cloned()
    }

    async fn put_job(&self, record: JobRecord) -> Result<(), String> {
        self.store.save(&record).await.map_err(|error| error.to_string())?;
        self.jobs.write().await.insert(record.run_id.clone(), record);
        Ok(())
    }

    fn make_run_id(&self, pipeline: &str) -> String {
        let name: String = pipeline.chars()
            .map(|ch| if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' { ch } else { '-' })
            .take(48)
            .collect();
        format!(
            "{}-{}-{}",
            Utc::now().format("%Y%m%dT%H%M%SZ"),
            if name.is_empty() { "pipeline" } else { &name },
            &uuid::Uuid::new_v4().simple().to_string()[..8]
        )
    }

    pub async fn handle(&self, request: IpcRequest) -> IpcResponse {
        match self.handle_inner(request).await {
            Ok(value) => IpcResponse::ok(value),
            Err((code, message)) => IpcResponse::error(code, message),
        }
    }

    async fn handle_inner(&self, request: IpcRequest) -> Result<Value, (String, String)> {
        match request {
            IpcRequest::Ping => Ok(json!({
                "service": "pipecraft",
                "version": env!("CARGO_PKG_VERSION"),
                "protocol": PROTOCOL_VERSION,
                "root": self.root,
                "worker_threads": self.config.worker_threads,
                "max_pipelines": self.config.max_pipelines,
                "max_tasks": self.config.max_tasks,
                "recovery": if self.config.recovery == RecoveryPolicy::Auto { "auto" } else { "manual" },
            })),
            IpcRequest::Submit { pipeline, execute, approve, labels, max_workers, from_step, only } => {
                let catalog = PipelineCatalog::open(&self.root).map_err(internal)?;
                let definition = catalog.load(&pipeline).map_err(|error| ("PIPELINE_NOT_FOUND".into(), error.to_string()))?;
                let errors = self.engine.validate(&definition);
                if !errors.is_empty() {
                    return Err(("PIPELINE_INVALID".into(), errors.join("; ")));
                }
                let run_id = self.make_run_id(&pipeline);
                let snapshot_path = self.persist_pipeline_snapshot(&run_id, &definition.path).await
                    .map_err(service_error)?;
                let request = JobRequest {
                    pipeline,
                    execute,
                    approve,
                    labels,
                    max_workers: max_workers.max(1),
                    from_step,
                    only,
                };
                let record = JobRecord::new(
                    run_id.clone(),
                    request,
                    &self.runs_dir,
                    &self.root,
                    snapshot_path.display().to_string(),
                );
                if let Err(error) = self.put_job(record).await {
                    let _ = tokio::fs::remove_file(&snapshot_path).await;
                    return Err(service_error(error));
                }
                self.start_existing(run_id.clone(), false).await.map_err(service_error)?;
                Ok(json!({"run_id": run_id, "status": "queued"}))
            }
            IpcRequest::Status { run_id } => {
                let record = self.get_job(&run_id).await.ok_or_else(|| not_found(&run_id))?;
                serde_json::to_value(record).map_err(internal)
            }
            IpcRequest::Jobs => {
                let jobs = self.jobs.read().await;
                let values = jobs.values().cloned().collect::<Vec<_>>();
                serde_json::to_value(values).map_err(internal)
            }
            IpcRequest::Cancel { run_id } => {
                let mut record = self.get_job(&run_id).await.ok_or_else(|| not_found(&run_id))?;
                if record.status.terminal() {
                    return Ok(json!({"run_id": run_id, "status": record.status, "already_terminal": true}));
                }
                if let Some(token) = self.cancellations.lock().await.get(&run_id).cloned() {
                    token.cancel();
                    if record.status == JobStatus::Queued {
                        record.status = JobStatus::Cancelled;
                        record.finished_at = Some(Utc::now().to_rfc3339());
                        record.message = "cancelled before execution".into();
                        record.touch();
                        self.put_job(record).await.map_err(service_error)?;
                        self.cancellations.lock().await.remove(&run_id);
                        return Ok(json!({"run_id": run_id, "cancel_requested": true, "status": "cancelled"}));
                    }
                    Ok(json!({"run_id": run_id, "cancel_requested": true}))
                } else {
                    Err(("JOB_NOT_ACTIVE".into(), format!("job {run_id} has no active cancellation token")))
                }
            }
            IpcRequest::Resume { run_id } => {
                let record = self.get_job(&run_id).await.ok_or_else(|| not_found(&run_id))?;
                if matches!(record.status, JobStatus::Queued | JobStatus::Running) {
                    return Err(("JOB_ALREADY_ACTIVE".into(), format!("job {run_id} is already active")));
                }
                if record.status == JobStatus::Succeeded {
                    return Ok(json!({"run_id": run_id, "status": "succeeded", "already_complete": true}));
                }
                let version_warning = if !record.runtime_version.is_empty() && record.runtime_version != env!("CARGO_PKG_VERSION") {
                    Some(format!("job was submitted under PipeCraft {} and is resuming under {}", record.runtime_version, env!("CARGO_PKG_VERSION")))
                } else { None };
                self.start_existing(run_id.clone(), true).await.map_err(service_error)?;
                Ok(json!({
                    "run_id": run_id,
                    "status": "queued",
                    "resume": true,
                    "warning": version_warning.unwrap_or_else(|| "the interrupted node is replayed; external side effects must be idempotent or verified by the domain executor".into())
                }))
            }
            IpcRequest::Events { run_id, after, limit } => self.read_events(&run_id, after, limit).await,
            IpcRequest::Report { run_id } => {
                let record = self.get_job(&run_id).await.ok_or_else(|| not_found(&run_id))?;
                let Some(path) = record.report_path else {
                    return Err(("REPORT_NOT_READY".into(), format!("job {run_id} has no final report yet")));
                };
                let bytes = tokio::fs::read(&path).await.map_err(internal)?;
                serde_json::from_slice(&bytes).map_err(internal)
            }
        }
    }

    async fn read_events(&self, run_id: &str, after: usize, limit: usize) -> Result<Value, (String, String)> {
        let record = self.get_job(run_id).await.ok_or_else(|| not_found(run_id))?;
        let file = match tokio::fs::File::open(&record.events_path).await {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(json!({"run_id": run_id, "after": after, "next": after, "events": []}));
            }
            Err(error) => return Err(internal(error)),
        };
        let mut lines = BufReader::new(file).lines();
        let mut index = 0usize;
        let mut events = Vec::new();
        let limit = limit.clamp(1, 1000);
        while let Some(line) = lines.next_line().await.map_err(internal)? {
            if index >= after && events.len() < limit {
                if let Ok(value) = serde_json::from_str::<Value>(&line) { events.push(value); }
            }
            index += 1;
            if events.len() >= limit { break; }
        }
        let next = if events.is_empty() { after } else { index };
        Ok(json!({"run_id": run_id, "after": after, "next": next, "events": events}))
    }

    async fn persist_pipeline_snapshot(&self, run_id: &str, source: &str) -> Result<PathBuf, String> {
        use tokio::io::AsyncWriteExt;
        let bytes = tokio::fs::read(source).await.map_err(|error| format!("could not read pipeline source {source}: {error}"))?;
        let run_dir = self.root.join(&self.runs_dir).join(run_id);
        tokio::fs::create_dir_all(&run_dir).await.map_err(|error| error.to_string())?;
        let path = run_dir.join("pipeline.snapshot.yaml");
        let tmp = run_dir.join(format!("pipeline.snapshot.yaml.tmp-{}", uuid::Uuid::new_v4().simple()));
        let mut file = tokio::fs::File::create(&tmp).await.map_err(|error| error.to_string())?;
        file.write_all(&bytes).await.map_err(|error| error.to_string())?;
        file.flush().await.map_err(|error| error.to_string())?;
        file.sync_all().await.map_err(|error| error.to_string())?;
        drop(file);
        tokio::fs::rename(&tmp, &path).await.map_err(|error| error.to_string())?;
        if let Ok(dir) = std::fs::File::open(&run_dir) { let _ = dir.sync_all(); }
        Ok(path)
    }

    async fn start_existing(&self, run_id: String, resume: bool) -> Result<(), String> {
        let mut record = self.get_job(&run_id).await.ok_or_else(|| format!("unknown job {run_id}"))?;
        let token = CancellationToken::new();
        self.cancellations.lock().await.insert(run_id.clone(), token.clone());
        if resume {
            record.status = JobStatus::Queued;
            record.message = "queued for resume".into();
            record.resume_requested = true;
            record.finished_at = None;
            record.report_path = None;
            record.warning = if !record.runtime_version.is_empty() && record.runtime_version != env!("CARGO_PKG_VERSION") {
                Some(format!(
                    "resuming a job submitted under PipeCraft {} with {}; interrupted nodes can be replayed",
                    record.runtime_version,
                    env!("CARGO_PKG_VERSION")
                ))
            } else {
                Some("an interrupted node can be replayed; exactly-once external side effects require domain-level idempotency or receipts".into())
            };
            record.touch();
            if let Err(error) = self.put_job(record.clone()).await {
                self.cancellations.lock().await.remove(&run_id);
                return Err(error);
            }
            let stale_latest = self.root.join(&record.runs_dir).join(&run_id).join("report.json");
            if let Err(error) = tokio::fs::remove_file(&stale_latest).await {
                if error.kind() != std::io::ErrorKind::NotFound {
                    self.cancellations.lock().await.remove(&run_id);
                    return Err(format!("could not clear prior latest report before resume: {error}"));
                }
            }
        }

        let service = self.clone();
        tokio::spawn(async move {
            let nested_service = service.clone();
            let nested_run_id = run_id.clone();
            let joined = tokio::spawn(async move {
                nested_service.execute_job(nested_run_id, resume, token).await
            }).await;
            if let Err(error) = joined {
                service.mark_internal_failure(&run_id, format!("runtime task panicked: {error}")).await;
            }
        });
        Ok(())
    }

    async fn execute_job(&self, run_id: String, resume: bool, token: CancellationToken) {
        let _permit = match self.pipeline_slots.clone().acquire_owned().await {
            Ok(permit) => permit,
            Err(_) => {
                self.mark_internal_failure(&run_id, "pipeline semaphore closed".into()).await;
                return;
            }
        };

        let Some(mut record) = self.get_job(&run_id).await else { return; };
        if record.status == JobStatus::Cancelled || token.is_cancelled() {
            self.cancellations.lock().await.remove(&run_id);
            return;
        }
        record.status = JobStatus::Running;
        record.started_at = Some(Utc::now().to_rfc3339());
        record.finished_at = None;
        record.attempts = record.attempts.saturating_add(1);
        record.resume_requested = resume;
        record.message = if resume { "resuming".into() } else { "running".into() };
        record.touch();
        if self.put_job(record.clone()).await.is_err() { return; }

        let definition = if !record.pipeline_snapshot_path.is_empty() {
            match load_pipeline(Path::new(&record.pipeline_snapshot_path)) {
                Ok(definition) => definition,
                Err(error) => {
                    self.mark_internal_failure(&run_id, format!("could not load durable pipeline snapshot: {error}")).await;
                    return;
                }
            }
        } else {
            let catalog = match PipelineCatalog::open(&self.root) {
                Ok(catalog) => catalog,
                Err(error) => {
                    self.mark_internal_failure(&run_id, error.to_string()).await;
                    return;
                }
            };
            match catalog.load(&record.request.pipeline) {
                Ok(definition) => definition,
                Err(error) => {
                    self.mark_internal_failure(&run_id, error.to_string()).await;
                    return;
                }
            }
        };
        let context = ExecutionContext::new(self.root.clone())
            .labels(record.request.labels.clone())
            .dry_run(!record.request.execute)
            .approve(record.request.approve)
            .runs_dir(record.runs_dir.clone())
            .from_step(record.request.from_step.clone())
            .only_steps(record.request.only.clone())
            .max_workers(record.request.max_workers)
            .resume(resume)
            .run_id(run_id.clone())
            .cancellation_token(token.clone());

        let mut run = self.engine
            .run_async_with_coordinator(&definition, &context, self.coordinator.clone())
            .await;
        let root = self.root.clone();
        let runs_dir = record.runs_dir.clone();
        let attempt = record.attempts;
        let write = tokio::task::spawn_blocking(move || {
            let path = write_run(&mut run, &root, &runs_dir)?;
            let attempt_dir = root.join(&runs_dir).join(&run.run_id).join("attempts");
            std::fs::create_dir_all(&attempt_dir)?;
            let attempt_path = attempt_dir.join(format!("attempt-{attempt:03}.json"));
            durable_copy(&path, &attempt_path)?;
            Ok::<_, std::io::Error>((run, path, attempt_path))
        }).await;

        let (run, report_path, attempt_path) = match write {
            Ok(Ok(value)) => value,
            Ok(Err(error)) => {
                self.mark_internal_failure(&run_id, format!("could not persist final report: {error}")).await;
                return;
            }
            Err(error) => {
                self.mark_internal_failure(&run_id, format!("report writer panicked: {error}")).await;
                return;
            }
        };

        if let Some(mut final_record) = self.get_job(&run_id).await {
            final_record.status = if token.is_cancelled() {
                JobStatus::Cancelled
            } else if run.success {
                JobStatus::Succeeded
            } else {
                JobStatus::Failed
            };
            final_record.finished_at = Some(Utc::now().to_rfc3339());
            final_record.message = match final_record.status {
                JobStatus::Succeeded => "completed successfully",
                JobStatus::Cancelled => "cancelled",
                _ => "completed with failures",
            }.into();
            final_record.report_path = Some(report_path.display().to_string());
            final_record.attempt_reports.push(attempt_path.display().to_string());
            final_record.touch();
            let _ = self.put_job(final_record).await;
        }
        self.cancellations.lock().await.remove(&run_id);
    }

    async fn mark_internal_failure(&self, run_id: &str, message: String) {
        if let Some(mut record) = self.get_job(run_id).await {
            record.status = JobStatus::Failed;
            record.finished_at = Some(Utc::now().to_rfc3339());
            record.message = message;
            record.touch();
            let _ = self.put_job(record).await;
        }
        self.cancellations.lock().await.remove(run_id);
    }

    async fn cancel_all(&self) {
        let tokens = self.cancellations.lock().await.values().cloned().collect::<Vec<_>>();
        for token in tokens { token.cancel(); }
    }

    async fn wait_for_active_shutdown(&self) {
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(8);
        while tokio::time::Instant::now() < deadline {
            if self.cancellations.lock().await.is_empty() { return; }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
    }

    #[cfg(unix)]
    pub async fn serve_unix(self, socket_path: PathBuf) -> Result<(), String> {
        use tokio::net::{UnixListener, UnixStream};
        if tokio::fs::try_exists(&socket_path).await.unwrap_or(false) {
            match UnixStream::connect(&socket_path).await {
                Ok(_) => return Err(format!("PipeCraft service already appears to be listening at {}", socket_path.display())),
                Err(_) => tokio::fs::remove_file(&socket_path).await.map_err(|error| error.to_string())?,
            }
        }
        if let Some(parent) = socket_path.parent() {
            tokio::fs::create_dir_all(parent).await.map_err(|error| error.to_string())?;
        }
        let listener = UnixListener::bind(&socket_path).map_err(|error| error.to_string())?;
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&socket_path, std::fs::Permissions::from_mode(0o600))
            .map_err(|error| error.to_string())?;
        loop {
            tokio::select! {
                accepted = listener.accept() => {
                    let (stream, _) = accepted.map_err(|error| error.to_string())?;
                    let service = self.clone();
                    tokio::spawn(async move { let _ = service.handle_stream(stream).await; });
                }
                signal = tokio::signal::ctrl_c() => {
                    signal.map_err(|error| error.to_string())?;
                    self.cancel_all().await;
                    self.wait_for_active_shutdown().await;
                    break;
                }
            }
        }
        drop(listener);
        let _ = tokio::fs::remove_file(&socket_path).await;
        Ok(())
    }

    #[cfg(not(unix))]
    pub async fn serve_tcp(self, address: &str) -> Result<(), String> {
        let listener = tokio::net::TcpListener::bind(address).await.map_err(|error| error.to_string())?;
        loop {
            tokio::select! {
                accepted = listener.accept() => {
                    let (stream, _) = accepted.map_err(|error| error.to_string())?;
                    let service = self.clone();
                    tokio::spawn(async move { let _ = service.handle_stream(stream).await; });
                }
                signal = tokio::signal::ctrl_c() => {
                    signal.map_err(|error| error.to_string())?;
                    self.cancel_all().await;
                    self.wait_for_active_shutdown().await;
                    break;
                }
            }
        }
        Ok(())
    }

    async fn handle_stream<S>(&self, stream: S) -> Result<(), String>
    where
        S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
    {
        let mut stream = stream;
        let mut bytes_in = Vec::with_capacity(4096);
        let mut chunk = [0u8; 8192];
        let mut saw_newline = false;
        while bytes_in.len() <= MAX_REQUEST_BYTES {
            let read = stream.read(&mut chunk).await.map_err(|error| error.to_string())?;
            if read == 0 { break; }
            if let Some(pos) = chunk[..read].iter().position(|byte| *byte == b'\n') {
                bytes_in.extend_from_slice(&chunk[..pos]);
                saw_newline = true;
                break;
            }
            bytes_in.extend_from_slice(&chunk[..read]);
        }
        let response = if bytes_in.is_empty() && !saw_newline {
            IpcResponse::error("EMPTY_REQUEST", "connection closed without a request")
        } else if bytes_in.len() > MAX_REQUEST_BYTES {
            IpcResponse::error("REQUEST_TOO_LARGE", "IPC request exceeds 1 MiB")
        } else if !saw_newline {
            IpcResponse::error("INVALID_REQUEST", "IPC request must end with a newline")
        } else {
            match serde_json::from_slice::<IpcRequest>(&bytes_in) {
                Ok(request) => self.handle(request).await,
                Err(error) => IpcResponse::error("INVALID_REQUEST", error.to_string()),
            }
        };
        let mut bytes = serde_json::to_vec(&response).map_err(|error| error.to_string())?;
        bytes.push(b'\n');
        stream.write_all(&bytes).await.map_err(|error| error.to_string())?;
        stream.shutdown().await.map_err(|error| error.to_string())?;
        Ok(())
    }
}

fn durable_copy(source: &Path, destination: &Path) -> std::io::Result<()> {
    use std::io::Write;
    if let Some(parent) = destination.parent() { std::fs::create_dir_all(parent)?; }
    let bytes = std::fs::read(source)?;
    let tmp = destination.with_extension(format!("tmp-{}", uuid::Uuid::new_v4().simple()));
    let mut file = std::fs::File::create(&tmp)?;
    file.write_all(&bytes)?;
    file.sync_all()?;
    drop(file);
    if let Err(first) = std::fs::rename(&tmp, destination) {
        if destination.exists() {
            std::fs::remove_file(destination)?;
            std::fs::rename(&tmp, destination)?;
        } else {
            return Err(first);
        }
    }
    if let Some(parent) = destination.parent() {
        if let Ok(dir) = std::fs::File::open(parent) { let _ = dir.sync_all(); }
    }
    Ok(())
}

fn internal(error: impl std::fmt::Display) -> (String, String) {
    ("INTERNAL_ERROR".into(), error.to_string())
}
fn service_error(message: String) -> (String, String) { ("SERVICE_ERROR".into(), message) }
fn not_found(run_id: &str) -> (String, String) { ("JOB_NOT_FOUND".into(), format!("unknown job {run_id}")) }
