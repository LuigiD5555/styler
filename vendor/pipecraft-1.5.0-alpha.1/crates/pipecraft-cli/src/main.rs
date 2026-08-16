//! `pipecraft` — the command-line interface.
//!
//! Intentionally small. It finds, validates, routes, plans and runs pipeline
//! definitions; everything a pipeline *does* lives in YAML, not here. This file
//! never imports a specific domain.

use std::path::{Path, PathBuf};

use clap::{Parser, Subcommand};
use serde_json::json;

use pipecraft_core::yamlx::WithMap;
use pipecraft_core::{extract_labels, load_routes, load_workspace, match_routes, select_route, PipelineCatalog};
use pipecraft_report::write_run;
use pipecraft_runtime::{
    CancellationToken, ExecutionContext, PipelineEngine, PipelineRequest, RuntimeLimits, RuntimeManager,
};
use pipecraft_service::{default_endpoint, IpcRequest, RecoveryPolicy, RuntimeService, ServiceClient, ServiceConfig, WorkspaceRuntimeLock};

#[derive(Parser)]
#[command(
    name = "pipecraft",
    version,
    about = "A local-first, polyglot, Git-native pipeline runtime driven by YAML."
)]
struct Cli {
    /// Workspace root (default: current directory).
    #[arg(long, global = true)]
    root: Option<PathBuf>,

    /// IPC endpoint. On Unix the default is .pipelines/pipecraft.sock.
    #[arg(long, global = true)]
    socket: Option<String>,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// List pipelines found in the workspace.
    List {
        /// Emit machine-readable JSON instead of human text.
        #[arg(long)]
        json: bool,
    },
    /// Validate a pipeline without running it.
    Validate {
        pipeline: String,
        /// Emit machine-readable JSON instead of human text.
        #[arg(long)]
        json: bool,
    },
    /// Show the execution order a pipeline would use, without running it.
    Plan {
        pipeline: String,
        /// Emit machine-readable JSON instead of human text.
        #[arg(long)]
        json: bool,
    },
    /// Preview which pipeline a set of labels would select.
    Route {
        labels: String,
        /// Emit machine-readable JSON instead of human text.
        #[arg(long)]
        json: bool,
    },
    /// Run a named pipeline (dry-run by default).
    Run {
        pipeline: String,
        #[arg(long, default_value = "")]
        labels: String,
        /// Actually execute side-effecting steps (default: dry-run).
        #[arg(long)]
        execute: bool,
        /// Approve gated steps (manual_approval, requires_approval, ...).
        #[arg(long)]
        approve: bool,
        /// Start execution at this step id in the resolved DAG order.
        #[arg(long)]
        from: Option<String>,
        /// Run only these comma-separated step ids, after applying --from if any.
        #[arg(long, value_delimiter = ',')]
        only: Vec<String>,
        /// Maximum concurrently running DAG nodes. Default 1 preserves V1 ordering.
        #[arg(long, default_value_t = 1)]
        max_workers: usize,
        /// Resume a prior run. Requires --run-id so state.json can be located.
        #[arg(long)]
        resume: bool,
        /// Explicit run id. Useful with --resume.
        #[arg(long)]
        run_id: Option<String>,
        /// Emit machine-readable JSON instead of human text.
        #[arg(long)]
        json: bool,
    },
    /// Run several named pipelines concurrently under one global Rust scheduler.
    #[command(name = "run-many")]
    RunMany {
        /// Pipeline names. Example: `pipecraft run-many build test docs`.
        pipelines: Vec<String>,
        /// Actually execute side-effecting steps (default: dry-run).
        #[arg(long)]
        execute: bool,
        /// Approve gated steps for every submitted pipeline.
        #[arg(long)]
        approve: bool,
        /// Per-pipeline concurrent DAG node limit.
        #[arg(long, default_value_t = 4)]
        max_workers: usize,
        /// Maximum pipelines executing at once.
        #[arg(long, default_value_t = 4)]
        max_pipelines: usize,
        /// Global concurrent task budget shared by all pipelines.
        #[arg(long, default_value_t = 16)]
        max_tasks: usize,
        /// Tokio worker threads. Zero selects available CPU parallelism.
        #[arg(long, default_value_t = 0)]
        worker_threads: usize,
        /// Emit machine-readable JSON.
        #[arg(long)]
        json: bool,
    },
    /// Start the long-lived local PipeCraft runtime service.
    Serve {
        /// Tokio worker threads. Zero selects available CPU parallelism.
        #[arg(long, default_value_t = 0)]
        worker_threads: usize,
        /// Maximum pipelines executing at once.
        #[arg(long, default_value_t = 8)]
        max_pipelines: usize,
        /// Global concurrent task budget shared by all jobs.
        #[arg(long, default_value_t = 32)]
        max_tasks: usize,
        /// Recovery policy after service restart: manual (safe default) or auto.
        #[arg(long, default_value = "manual")]
        recovery: String,
    },
    /// Submit a pipeline to the resident runtime service and return immediately.
    Submit {
        pipeline: String,
        #[arg(long)]
        execute: bool,
        #[arg(long)]
        approve: bool,
        #[arg(long, value_delimiter = ',')]
        labels: Vec<String>,
        #[arg(long, default_value_t = 4)]
        max_workers: usize,
        #[arg(long)]
        from: Option<String>,
        #[arg(long, value_delimiter = ',')]
        only: Vec<String>,
        #[arg(long)]
        json: bool,
    },
    /// Show durable service state for one run id.
    Status {
        run_id: String,
        #[arg(long)]
        json: bool,
    },
    /// List durable jobs known by the runtime service.
    Jobs {
        #[arg(long)]
        json: bool,
    },
    /// Cooperatively cancel an active service job.
    Cancel {
        run_id: String,
        #[arg(long)]
        json: bool,
    },
    /// Resume an interrupted or failed job using its persisted state.
    #[command(name = "resume-job")]
    ResumeJob {
        run_id: String,
        #[arg(long)]
        json: bool,
    },
    /// Read structured persisted events for a service job.
    #[command(name = "job-events")]
    JobEvents {
        run_id: String,
        #[arg(long, default_value_t = 0)]
        after: usize,
        #[arg(long, default_value_t = 200)]
        limit: usize,
        #[arg(long)]
        json: bool,
    },
    /// Read the final report for a completed service job.
    #[command(name = "job-report")]
    JobReport {
        run_id: String,
        #[arg(long)]
        json: bool,
    },
    /// Route labels to a pipeline, then run it.
    #[command(name = "run-labels")]
    RunLabels {
        labels: String,
        #[arg(long)]
        execute: bool,
        #[arg(long)]
        approve: bool,
        /// Start execution at this step id in the resolved DAG order.
        #[arg(long)]
        from: Option<String>,
        /// Run only these comma-separated step ids, after applying --from if any.
        #[arg(long, value_delimiter = ',')]
        only: Vec<String>,
        #[arg(long, default_value_t = 1)]
        max_workers: usize,
        #[arg(long)]
        resume: bool,
        #[arg(long)]
        run_id: Option<String>,
        /// Emit machine-readable JSON instead of human text.
        #[arg(long)]
        json: bool,
    },
}

fn resolve_root(root: &Option<PathBuf>) -> PathBuf {
    root.clone()
        .map(|p| p.canonicalize().unwrap_or(p))
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")))
}

fn main() {
    let cli = Cli::parse();
    let root = resolve_root(&cli.root);
    let code = match run(&cli.command, &root, cli.socket.as_deref()) {
        Ok(code) => code,
        Err(e) => {
            eprintln!("  ⚠️  {e}");
            1
        }
    };
    std::process::exit(code);
}

fn print_json(value: serde_json::Value) -> anyhow::Result<()> {
    println!("{}", serde_json::to_string_pretty(&value)?);
    Ok(())
}

fn run(command: &Commands, root: &Path, socket: Option<&str>) -> anyhow::Result<i32> {
    match command {
        Commands::List { json } => cmd_list(root, *json),
        Commands::Validate { pipeline, json } => cmd_validate(root, pipeline, *json),
        Commands::Plan { pipeline, json } => cmd_plan(root, pipeline, *json),
        Commands::Route { labels, json } => cmd_route(root, labels, *json),
        Commands::Run {
            pipeline,
            labels,
            execute,
            approve,
            from,
            only,
            max_workers,
            resume,
            run_id,
            json,
        } => cmd_run(
            root,
            pipeline,
            labels,
            *execute,
            *approve,
            from.clone(),
            only.clone(),
            *max_workers,
            *resume,
            run_id.clone(),
            None,
            *json,
        ),
        Commands::RunMany {
            pipelines,
            execute,
            approve,
            max_workers,
            max_pipelines,
            max_tasks,
            worker_threads,
            json,
        } => cmd_run_many(
            root,
            pipelines,
            *execute,
            *approve,
            *max_workers,
            *max_pipelines,
            *max_tasks,
            *worker_threads,
            *json,
        ),
        Commands::Serve { worker_threads, max_pipelines, max_tasks, recovery } => {
            cmd_serve(root, socket, *worker_threads, *max_pipelines, *max_tasks, recovery)
        }
        Commands::Submit { pipeline, execute, approve, labels, max_workers, from, only, json } => {
            service_command(root, socket, IpcRequest::Submit {
                pipeline: pipeline.clone(), execute: *execute, approve: *approve,
                labels: labels.clone(), max_workers: *max_workers,
                from_step: from.clone(), only: only.clone(),
            }, *json)
        }
        Commands::Status { run_id, json } => service_command(root, socket, IpcRequest::Status { run_id: run_id.clone() }, *json),
        Commands::Jobs { json } => service_command(root, socket, IpcRequest::Jobs, *json),
        Commands::Cancel { run_id, json } => service_command(root, socket, IpcRequest::Cancel { run_id: run_id.clone() }, *json),
        Commands::ResumeJob { run_id, json } => service_command(root, socket, IpcRequest::Resume { run_id: run_id.clone() }, *json),
        Commands::JobEvents { run_id, after, limit, json } => service_command(root, socket, IpcRequest::Events { run_id: run_id.clone(), after: *after, limit: *limit }, *json),
        Commands::JobReport { run_id, json } => service_command(root, socket, IpcRequest::Report { run_id: run_id.clone() }, *json),
        Commands::RunLabels {
            labels,
            execute,
            approve,
            from,
            only,
            max_workers,
            resume,
            run_id,
            json,
        } => cmd_run_labels(root, labels, *execute, *approve, from.clone(), only.clone(), *max_workers, *resume, run_id.clone(), *json),
    }
}


fn service_endpoint(root: &Path, socket: Option<&str>) -> String {
    socket.map(str::to_string).unwrap_or_else(|| default_endpoint(root))
}

fn service_command(root: &Path, socket: Option<&str>, request: IpcRequest, emit_json: bool) -> anyhow::Result<i32> {
    let endpoint = service_endpoint(root, socket);
    let client = ServiceClient::new(endpoint.clone());
    let response = client.request(&request)
        .map_err(|error| anyhow::anyhow!("could not contact PipeCraft service at {endpoint}: {error}"))?;
    if emit_json {
        println!("{}", serde_json::to_string_pretty(&response)?);
    } else if response.ok {
        if let Some(data) = response.data.as_ref() {
            println!("{}", serde_json::to_string_pretty(data)?);
        }
    } else if let Some(error) = response.error.as_ref() {
        eprintln!("  ⚠️  {}: {}", error.code, error.message);
    }
    Ok(if response.ok { 0 } else { 1 })
}

fn cmd_serve(
    root: &Path,
    socket: Option<&str>,
    worker_threads: usize,
    max_pipelines: usize,
    max_tasks: usize,
    recovery: &str,
) -> anyhow::Result<i32> {
    let cores = std::thread::available_parallelism().map(|value| value.get()).unwrap_or(4);
    let recovery = match recovery {
        "manual" => RecoveryPolicy::Manual,
        "auto" => RecoveryPolicy::Auto,
        other => return Err(anyhow::anyhow!("unknown recovery policy {other:?}; use manual or auto")),
    };
    let config = ServiceConfig {
        worker_threads: if worker_threads == 0 { cores.max(2) } else { worker_threads.max(1) },
        max_pipelines: max_pipelines.max(1),
        max_tasks: max_tasks.max(1),
        recovery,
    };
    let endpoint = service_endpoint(root, socket);
    println!("  PipeCraft runtime service  : {endpoint}");
    println!("  Recovery policy            : {}", if recovery == RecoveryPolicy::Auto { "auto" } else { "manual" });
    println!("  Max pipelines / tasks      : {} / {}", config.max_pipelines, config.max_tasks);

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(config.worker_threads.max(1))
        .enable_all()
        .build()?;
    let result = runtime.block_on(async {
        let service = RuntimeService::open(root.to_path_buf(), config).await
            .map_err(anyhow::Error::msg)?;
        #[cfg(unix)]
        { service.serve_unix(PathBuf::from(endpoint)).await.map_err(anyhow::Error::msg) }
        #[cfg(not(unix))]
        { service.serve_tcp(endpoint.strip_prefix("tcp://").unwrap_or(&endpoint)).await.map_err(anyhow::Error::msg) }
    });
    result?;
    Ok(0)
}

fn cmd_list(root: &Path, emit_json: bool) -> anyhow::Result<i32> {
    let catalog = PipelineCatalog::open(root)?;
    let names = catalog.list();

    if emit_json {
        let is_empty = names.is_empty();
        print_json(json!({
            "workspace": catalog.workspace.name,
            "pipelines": names,
        }))?;
        return Ok(if is_empty { 1 } else { 0 });
    }

    println!("\n  workspace      : {}", catalog.workspace.name);
    println!("  pipeline dir   : {}\n", catalog.workspace.pipeline_dir);
    if names.is_empty() {
        println!("  No pipelines found. Add YAML files under {}/", catalog.workspace.pipeline_dir);
        return Ok(1);
    }
    for name in names {
        println!("  - {name}");
    }
    Ok(0)
}

fn cmd_validate(root: &Path, pipeline: &str, emit_json: bool) -> anyhow::Result<i32> {
    let catalog = PipelineCatalog::open(root)?;
    let def = match catalog.load(pipeline) {
        Ok(d) => d,
        Err(e) => {
            if emit_json {
                print_json(json!({
                    "pipeline": pipeline,
                    "valid": false,
                    "errors": [e.to_string()],
                }))?;
            } else {
                println!("  ⚠️  {e}");
            }
            return Ok(1);
        }
    };
    let errors = PipelineEngine::default().validate(&def);

    if emit_json {
        print_json(json!({
            "pipeline": def.name,
            "valid": errors.is_empty(),
            "errors": errors,
        }))?;
        return Ok(if errors.is_empty() { 0 } else { 1 });
    }

    if errors.is_empty() {
        println!("  ✅ Pipeline valid: {} ({} steps)", def.name, def.steps.len());
        Ok(0)
    } else {
        println!("\n  Pipeline invalid: {}", def.name);
        for e in errors {
            println!("  - {e}");
        }
        Ok(1)
    }
}

fn cmd_plan(root: &Path, pipeline: &str, emit_json: bool) -> anyhow::Result<i32> {
    let catalog = PipelineCatalog::open(root)?;
    let def = match catalog.load(pipeline) {
        Ok(d) => d,
        Err(e) => {
            if emit_json {
                print_json(json!({
                    "pipeline": pipeline,
                    "order": [],
                    "errors": [e.to_string()],
                }))?;
                return Ok(1);
            }
            return Err(e.into());
        }
    };
    match PipelineEngine::default().plan(&def) {
        Ok(order) => {
            if emit_json {
                print_json(json!({
                    "pipeline": def.name,
                    "order": order,
                }))?;
                return Ok(0);
            }

            println!("\n  Plan for: {}", def.name);
            println!("  Execution order ({} steps):", order.len());
            for (i, id) in order.iter().enumerate() {
                let step = def.steps.iter().find(|s| &s.id == id).unwrap();
                let gate = if step.requires_approval { " [approval]" } else { "" };
                println!("  {:>2}. {id}  ({}){gate}", i + 1, step.step_type);
            }
            Ok(0)
        }
        Err(errors) => {
            if emit_json {
                print_json(json!({
                    "pipeline": def.name,
                    "order": [],
                    "errors": errors,
                }))?;
                return Ok(1);
            }

            println!("\n  Pipeline invalid: {}", def.name);
            for e in errors {
                println!("  - {e}");
            }
            Ok(1)
        }
    }
}

fn cmd_route(root: &Path, labels_text: &str, emit_json: bool) -> anyhow::Result<i32> {
    let ws = load_workspace(root)?;
    let labels = extract_labels(labels_text);
    let routes = load_routes(root, Some(&ws))?;
    let matches = match_routes(&labels, &routes);
    let (selected, warnings) = select_route(&labels, &routes, &ws.default_pipeline);

    if emit_json {
        let matched_routes: Vec<_> = matches
            .iter()
            .map(|m| json!({ "id": m.route.id, "pipeline": m.pipeline }))
            .collect();
        let selected_pipeline = selected.as_ref().map(|m| m.pipeline.clone());
        print_json(json!({
            "labels": labels,
            "selected_pipeline": selected_pipeline,
            "matched_routes": matched_routes,
            "warnings": warnings,
        }))?;
        return Ok(if selected.is_some() { 0 } else { 1 });
    }

    let shown = if labels.is_empty() { "—".to_string() } else { labels.join(" ") };
    println!("\n  Labels: {shown}");
    if !warnings.is_empty() {
        println!("  Warnings:");
        for w in &warnings {
            println!("  - {w}");
        }
    }
    if matches.is_empty() {
        let sel = selected.map(|m| m.pipeline).unwrap_or_else(|| ws.default_pipeline.clone());
        println!("  Selected pipeline: {sel}");
        return Ok(0);
    }
    println!("  Matched routes:");
    let selected_id = selected.as_ref().map(|m| m.route.id.clone());
    for m in &matches {
        let marker = if selected_id.as_deref() == Some(&m.route.id) { "*" } else { " " };
        println!("  {marker} {} → {}", m.route.id, m.pipeline);
    }
    Ok(0)
}

fn cmd_run(
    root: &Path,
    pipeline: &str,
    labels_text: &str,
    execute: bool,
    approve: bool,
    from: Option<String>,
    only: Vec<String>,
    max_workers: usize,
    resume: bool,
    run_id: Option<String>,
    context: Option<WithMap>,
    emit_json: bool,
) -> anyhow::Result<i32> {
    let catalog = PipelineCatalog::open(root)?;
    let _runtime_lock = WorkspaceRuntimeLock::acquire(&root.join(".pipelines/runtime"))
        .map_err(|error| anyhow::anyhow!("cannot start one-shot runtime: {error}; if `pipecraft serve` is active, use `pipecraft submit`"))?;
    if resume && run_id.as_deref().map(str::trim).unwrap_or("").is_empty() {
        if emit_json {
            print_json(json!({
                "pipeline": pipeline,
                "run_id": null,
                "success": false,
                "errors": ["--resume requires --run-id"],
            }))?;
        } else {
            eprintln!("  ⚠️  --resume requires --run-id");
        }
        return Ok(1);
    }
    let labels = extract_labels(labels_text);
    let def = match catalog.load(pipeline) {
        Ok(d) => d,
        Err(e) => {
            if emit_json {
                print_json(json!({
                    "pipeline": pipeline,
                    "run_id": null,
                    "success": false,
                    "dry_run": !execute,
                    "report_path": null,
                    "logs_dir": null,
                    "artifacts_dir": null,
                    "errors": [e.to_string()],
                }))?;
            } else {
                println!("  ⚠️  {e}");
            }
            return Ok(1);
        }
    };

    let cancellation = CancellationToken::new();
    let signal_token = cancellation.clone();
    if let Err(e) = ctrlc::set_handler(move || signal_token.cancel()) {
        if !emit_json { eprintln!("  ⚠️  could not install Ctrl+C handler: {e}"); }
    }

    let ctx = ExecutionContext::new(root.to_path_buf())
        .labels(labels)
        .dry_run(!execute)
        .approve(approve)
        .runs_dir(catalog.workspace.runs_dir.clone())
        .context(context.unwrap_or_default())
        .from_step(from)
        .only_steps(only)
        .max_workers(max_workers)
        .resume(resume)
        .run_id(run_id.unwrap_or_default())
        .cancellation_token(cancellation);

    let engine = PipelineEngine::default();
    let mut run = engine.run(&def, &ctx);
    let path = write_run(&mut run, root, &catalog.workspace.runs_dir)?;

    if emit_json {
        print_json(json!({
            "pipeline": run.pipeline,
            "run_id": run.run_id,
            "success": run.success,
            "dry_run": run.dry_run,
            "report_path": path.display().to_string(),
            "logs_dir": run.logs_dir,
            "artifacts_dir": run.artifacts_dir,
            "events_path": run.events_path,
            "state_path": run.state_path,
            "plan_fingerprint": run.plan_fingerprint,
            "max_workers": run.max_workers,
        }))?;
        return Ok(if run.success { 0 } else { 1 });
    }

    println!("\n  Pipeline : {}", run.pipeline);
    println!("  Mode     : {}", if run.dry_run { "dry-run" } else { "execute" });
    println!("  Status   : {}", if run.success { "success" } else { "needs attention" });
    if !run.selected_from.is_empty() {
        println!("  From     : {}", run.selected_from);
    }
    if !run.selected_only.is_empty() {
        println!("  Only     : {}", run.selected_only.join(", "));
    }
    println!("  Report   : {}", path.display());
    println!("  Logs     : {}", run.logs_dir);
    println!("  Artifacts: {}", run.artifacts_dir);
    println!("  Events   : {}", run.events_path);
    println!("  State    : {}", run.state_path);
    println!("  Workers  : {}", run.max_workers);
    println!("\n  Steps:");
    for r in &run.results {
        let icon = if r.success {
            "✅"
        } else if r.status == "needs_approval" {
            "⏸"
        } else {
            "❌"
        };
        println!("  {icon} {:24} [{}] {} — {}", r.step_id, r.step_type, r.status, r.message);
    }
    Ok(if run.success { 0 } else { 1 })
}

fn cmd_run_many(
    root: &Path,
    pipelines: &[String],
    execute: bool,
    approve: bool,
    max_workers: usize,
    max_pipelines: usize,
    max_tasks: usize,
    worker_threads: usize,
    emit_json: bool,
) -> anyhow::Result<i32> {
    if pipelines.is_empty() {
        if emit_json {
            print_json(json!({"success": false, "errors": ["run-many requires at least one pipeline"]}))?;
        } else {
            eprintln!("  ⚠️  run-many requires at least one pipeline");
        }
        return Ok(1);
    }

    let catalog = PipelineCatalog::open(root)?;
    let _runtime_lock = WorkspaceRuntimeLock::acquire(&root.join(".pipelines/runtime"))
        .map_err(|error| anyhow::anyhow!("cannot start one-shot runtime: {error}; if `pipecraft serve` is active, use `pipecraft submit`"))?;
    let cancellation = CancellationToken::new();
    let signal_token = cancellation.clone();
    if let Err(error) = ctrlc::set_handler(move || signal_token.cancel()) {
        if !emit_json {
            eprintln!("  ⚠️  could not install Ctrl+C handler: {error}");
        }
    }

    let mut requests = Vec::with_capacity(pipelines.len());
    for name in pipelines {
        let definition = match catalog.load(name) {
            Ok(definition) => definition,
            Err(error) => {
                if emit_json {
                    print_json(json!({
                        "success": false,
                        "errors": [error.to_string()],
                        "pipeline": name,
                    }))?;
                    return Ok(1);
                }
                return Err(error.into());
            }
        };
        let context = ExecutionContext::new(root.to_path_buf())
            .dry_run(!execute)
            .approve(approve)
            .runs_dir(catalog.workspace.runs_dir.clone())
            .max_workers(max_workers)
            .cancellation_token(cancellation.clone());
        requests.push(PipelineRequest::new(definition, context));
    }

    let cores = std::thread::available_parallelism().map(|value| value.get()).unwrap_or(4);
    let limits = RuntimeLimits {
        worker_threads: if worker_threads == 0 { cores.max(2) } else { worker_threads.max(1) },
        max_pipelines: max_pipelines.max(1),
        max_tasks: max_tasks.max(1),
    };
    let manager = RuntimeManager::new(PipelineEngine::default(), limits.clone());
    let mut runs = manager.run_many(requests);

    let mut run_json = Vec::with_capacity(runs.len());
    let mut all_success = true;
    for run in &mut runs {
        let path = write_run(run, root, &catalog.workspace.runs_dir)?;
        all_success &= run.success;
        run_json.push(json!({
            "pipeline": run.pipeline,
            "run_id": run.run_id,
            "success": run.success,
            "report_path": path.display().to_string(),
            "events_path": run.events_path,
            "state_path": run.state_path,
        }));
    }

    if emit_json {
        print_json(json!({
            "success": all_success,
            "runtime": "rust-tokio",
            "worker_threads": limits.worker_threads,
            "max_pipelines": limits.max_pipelines,
            "max_tasks": limits.max_tasks,
            "runs": run_json,
        }))?;
        return Ok(if all_success { 0 } else { 1 });
    }

    println!("\n  Runtime       : rust-tokio");
    println!("  Pipelines     : {}", runs.len());
    println!("  Worker threads: {}", limits.worker_threads);
    println!("  Max pipelines : {}", limits.max_pipelines);
    println!("  Global tasks  : {}", limits.max_tasks);
    for run in &runs {
        println!(
            "  {} {}  {}",
            if run.success { "✅" } else { "⚠️" },
            run.pipeline,
            run.run_id
        );
    }
    Ok(if all_success { 0 } else { 1 })
}

fn cmd_run_labels(
    root: &Path,
    labels_text: &str,
    execute: bool,
    approve: bool,
    from: Option<String>,
    only: Vec<String>,
    max_workers: usize,
    resume: bool,
    run_id: Option<String>,
    emit_json: bool,
) -> anyhow::Result<i32> {
    let ws = load_workspace(root)?;
    let routes = load_routes(root, Some(&ws))?;
    let labels = extract_labels(labels_text);
    let (selected, warnings) = select_route(&labels, &routes, &ws.default_pipeline);

    if !emit_json {
        for w in &warnings {
            println!("  ⚠️  {w}");
        }
    }

    let selected = match selected {
        Some(s) => s,
        None => {
            if emit_json {
                print_json(json!({
                    "labels": labels,
                    "selected_pipeline": null,
                    "success": false,
                    "warnings": warnings,
                    "errors": ["no route selected"],
                }))?;
            }
            return Ok(1);
        }
    };
    if !emit_json && selected.route.id != "__default__" {
        println!("  Route: {} → {}", selected.route.id, selected.pipeline);
    }
    cmd_run(
        root,
        &selected.pipeline,
        labels_text,
        execute,
        approve,
        from,
        only,
        max_workers,
        resume,
        run_id,
        Some(selected.context.clone()),
        emit_json,
    )
}
