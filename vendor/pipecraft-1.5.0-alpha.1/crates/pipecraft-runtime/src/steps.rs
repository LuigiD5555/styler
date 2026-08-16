//! Built-in V1/V1.1 step executors: note, checklist, manual_approval, command,
//! file_check, target_plan, boundary_check, git_diff, copy_or_sync and plugin.
//!
//! Each is project-agnostic. None of them knows what "Pro", "Lite", "Odoo" or
//! "deploy" means — that meaning lives entirely in the YAML the author writes.

use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Output, Stdio};
use std::time::{Duration, Instant};

use globset::{Glob, GlobSetBuilder, GlobSet};
use walkdir::WalkDir;

use pipecraft_core::model::{PipelineDefinition, PipelineStep};
use pipecraft_core::yamlx;
use pipecraft_report::{status, StepResult};

use crate::context::ExecutionContext;
use crate::events::emit_event;
use crate::executor::{error_result, error_result_from_config, RepositoryResolver, StepExecutor, StepFuture};
use crate::process::run_process_with_retries_async;

const DEFAULT_IGNORE: &[&str] = &[
    ".git/**",
    ".pipelines/**",
    "tests/**",
    "__pycache__/**",
    ".pytest_cache/**",
    "target/**",
    "node_modules/**",
];
const DEFAULT_SCAN_MAX_BYTES: u64 = 1_000_000;
const DEFAULT_MAX_FINDINGS: usize = 50;

// ── note ────────────────────────────────────────────────────────────────────

pub struct NoteExecutor;
impl StepExecutor for NoteExecutor {
    fn step_type(&self) -> &'static str {
        "note"
    }
    fn run(&self, step: &PipelineStep, _p: &PipelineDefinition, _c: &ExecutionContext) -> StepResult {
        let msg = if !step.description.is_empty() {
            step.description.clone()
        } else {
            step.with.get("text").and_then(yamlx::as_string).unwrap_or_else(|| "note".into())
        };
        StepResult::new(&step.id, &step.step_type, true, status::OK, msg)
    }
}

// ── checklist ───────────────────────────────────────────────────────────────

pub struct ChecklistExecutor;
impl StepExecutor for ChecklistExecutor {
    fn step_type(&self) -> &'static str {
        "checklist"
    }
    fn run(&self, step: &PipelineStep, _p: &PipelineDefinition, _c: &ExecutionContext) -> StepResult {
        let items = match step.with.get("items") {
            Some(v) if v.is_sequence() => v.as_sequence().unwrap().clone(),
            None => Vec::new(),
            Some(_) => {
                return error_result(
                    step,
                    "checklist items must be a list",
                    "CHECKLIST_CONFIG_ERROR",
                    "Use `with: items: [- item]`.",
                )
            }
        };
        let lines: Vec<String> = items
            .iter()
            .map(|i| format!("- [ ] {}", yamlx::as_string(i).unwrap_or_default()))
            .collect();
        StepResult::new(
            &step.id,
            &step.step_type,
            true,
            status::OK,
            format!("{} checklist items", items.len()),
        )
        .with_output(lines.join("\n"))
    }
}

// ── manual_approval ─────────────────────────────────────────────────────────

pub struct ManualApprovalExecutor;
impl StepExecutor for ManualApprovalExecutor {
    fn step_type(&self) -> &'static str {
        "manual_approval"
    }
    fn run(&self, step: &PipelineStep, _p: &PipelineDefinition, ctx: &ExecutionContext) -> StepResult {
        if ctx.approve {
            StepResult::new(&step.id, &step.step_type, true, status::OK, "approval supplied")
        } else {
            StepResult::new(
                &step.id,
                &step.step_type,
                false,
                status::NEEDS_APPROVAL,
                "manual approval required",
            )
        }
    }
}

// ── command ─────────────────────────────────────────────────────────────────

pub struct CommandExecutor;
impl StepExecutor for CommandExecutor {
    fn step_type(&self) -> &'static str {
        "command"
    }
    fn run_async<'a>(&'a self, step: &'a PipelineStep, pipeline: &'a PipelineDefinition, ctx: &'a ExecutionContext) -> StepFuture<'a> {
        Box::pin(run_command_async(step, pipeline, ctx))
    }
    fn run(&self, step: &PipelineStep, pipeline: &PipelineDefinition, ctx: &ExecutionContext) -> StepResult {
        if step.requires_approval && !ctx.approve {
            return StepResult::new(
                &step.id,
                &step.step_type,
                false,
                status::NEEDS_APPROVAL,
                "command requires approval",
            );
        }

        let repo_root = match RepositoryResolver::new(&ctx.root, pipeline).resolve(&step.repo) {
            Ok(p) => p,
            Err(e) => return error_result_from_config(step, &e),
        };

        let argv = argv_from_step(step);
        let shell_command: Option<String> = step
            .with
            .get("command")
            .and_then(yamlx::as_string)
            .or_else(|| if step.command.is_empty() { None } else { Some(step.command.clone()) });

        let (display, is_shell) = match (&argv, &shell_command) {
            (Some(a), _) => (a.join(" "), false),
            (None, Some(c)) => (c.clone(), true),
            (None, None) => {
                return error_result(
                    step,
                    "command step has no argv or command",
                    "COMMAND_CONFIG_ERROR",
                    "Set `with.argv: [...]` (preferred) or `with.command: \"...\"`.",
                )
            }
        };

        let timeout = u64_field(step, "timeout");
        let retries = u64_field(step, "retries").unwrap_or(0);
        let retry_delay = u64_field(step, "retry_delay").unwrap_or(0);

        if ctx.dry_run {
            let mut out = format!("cwd: {}\ncommand: {display}", repo_root.display());
            if is_shell {
                out.push_str("\nmode: shell (sh -c) — riskier than argv");
            } else {
                out.push_str("\nmode: argv (no shell)");
            }
            if let Some(t) = timeout {
                out.push_str(&format!("\ntimeout: {t}s"));
            }
            if retries > 0 {
                out.push_str(&format!("\nretries: {retries}, retry_delay: {retry_delay}s"));
            }
            out.push_str(&format!("\nlogs_dir: {}", ctx.logs_dir.display()));
            return StepResult::new(
                &step.id,
                &step.step_type,
                true,
                status::DRY_RUN,
                "command not executed",
            )
            .with_output(out)
            .with_data(serde_json::json!({
                "cwd": repo_root.display().to_string(),
                "command": display,
                "shell": is_shell,
                "timeout_seconds": timeout,
                "retries": retries,
                "retry_delay_seconds": retry_delay,
            }));
        }

        run_process_with_retries(step, pipeline, ctx, &repo_root, argv, shell_command, timeout, retries, retry_delay, None)
    }
}

async fn run_command_async(
    step: &PipelineStep,
    pipeline: &PipelineDefinition,
    ctx: &ExecutionContext,
) -> StepResult {
    if step.requires_approval && !ctx.approve {
        return StepResult::new(
            &step.id,
            &step.step_type,
            false,
            status::NEEDS_APPROVAL,
            "command requires approval",
        );
    }
    let repo_root = match RepositoryResolver::new(&ctx.root, pipeline).resolve(&step.repo) {
        Ok(path) => path,
        Err(error) => return error_result_from_config(step, &error),
    };
    let argv = argv_from_step(step);
    let shell_command = step
        .with
        .get("command")
        .and_then(yamlx::as_string)
        .or_else(|| if step.command.is_empty() { None } else { Some(step.command.clone()) });
    let (display, is_shell) = match (&argv, &shell_command) {
        (Some(args), _) => (args.join(" "), false),
        (None, Some(command)) => (command.clone(), true),
        (None, None) => {
            return error_result(
                step,
                "command step has no argv or command",
                "COMMAND_CONFIG_ERROR",
                "Set `with.argv: [...]` (preferred) or `with.command: \"...\"`.",
            )
        }
    };
    let timeout = u64_field(step, "timeout");
    let retries = u64_field(step, "retries").unwrap_or(0);
    let retry_delay = u64_field(step, "retry_delay").unwrap_or(0);
    if ctx.dry_run {
        let mut output = format!("cwd: {}\ncommand: {display}", repo_root.display());
        output.push_str(if is_shell { "\nmode: shell (sh -c) — riskier than argv" } else { "\nmode: argv (no shell)" });
        if let Some(seconds) = timeout {
            output.push_str(&format!("\ntimeout: {seconds}s"));
        }
        if let Some(seconds) = u64_field(step, "inactivity_timeout") {
            output.push_str(&format!("\ninactivity_timeout: {seconds}s"));
        }
        if retries > 0 {
            output.push_str(&format!("\nretries: {retries}, retry_delay: {retry_delay}s"));
        }
        output.push_str(&format!("\nlogs_dir: {}", ctx.logs_dir.display()));
        return StepResult::new(&step.id, &step.step_type, true, status::DRY_RUN, "command not executed")
            .with_output(output)
            .with_data(serde_json::json!({
                "cwd": repo_root.display().to_string(),
                "command": display,
                "shell": is_shell,
                "timeout_seconds": timeout,
                "inactivity_timeout_seconds": u64_field(step, "inactivity_timeout"),
                "retries": retries,
                "retry_delay_seconds": retry_delay,
                "process_runtime": "tokio",
            }));
    }
    run_process_with_retries_async(
        step,
        pipeline,
        ctx,
        &repo_root,
        argv,
        shell_command,
        timeout,
        retries,
        retry_delay,
        None,
    )
    .await
}

// ── plugin ──────────────────────────────────────────────────────────────────

/// Polyglot external plugin protocol.
///
/// A plugin receives JSON on stdin and may return JSON on stdout:
/// `{ "success": true, "status": "ok", "message": "...", "output": "...", "data": {...} }`.
/// If stdout is not JSON, PipeCraft falls back to command-like status handling.
pub struct PluginExecutor;
impl StepExecutor for PluginExecutor {
    fn step_type(&self) -> &'static str {
        "plugin"
    }
    fn run_async<'a>(&'a self, step: &'a PipelineStep, pipeline: &'a PipelineDefinition, ctx: &'a ExecutionContext) -> StepFuture<'a> {
        Box::pin(run_plugin_async(step, pipeline, ctx))
    }
    fn run(&self, step: &PipelineStep, pipeline: &PipelineDefinition, ctx: &ExecutionContext) -> StepResult {
        let repo_root = match RepositoryResolver::new(&ctx.root, pipeline).resolve(&step.repo) {
            Ok(p) => p,
            Err(e) => return error_result_from_config(step, &e),
        };
        let argv = argv_from_step(step);
        let timeout = u64_field(step, "timeout");
        let retries = u64_field(step, "retries").unwrap_or(0);
        let retry_delay = u64_field(step, "retry_delay").unwrap_or(0);
        let payload = serde_json::json!({
            "protocol": "pipecraft.plugin/v1",
            "run_id": ctx.run_id,
            "pipeline": pipeline.name,
            "step_id": step.id,
            "step_type": step.step_type,
            "root": ctx.root.display().to_string(),
            "cwd": repo_root.display().to_string(),
            "labels": ctx.labels,
            "dry_run": ctx.dry_run,
            "with": serde_json::to_value(&step.with).unwrap_or_default(),
            "context": serde_json::to_value(&ctx.context).unwrap_or_default(),
            "artifacts_dir": ctx.artifacts_dir.display().to_string(),
            "logs_dir": ctx.logs_dir.display().to_string(),
        });
        if ctx.dry_run {
            let display = argv.clone().unwrap_or_default().join(" ");
            return StepResult::new(&step.id, &step.step_type, true, status::DRY_RUN, "plugin not executed")
                .with_output(format!("cwd: {}\nplugin: {display}\nprotocol: pipecraft.plugin/v1", repo_root.display()))
                .with_data(serde_json::json!({ "payload_preview": payload }));
        }
        let result = run_process_with_retries(
            step,
            pipeline,
            ctx,
            &repo_root,
            argv,
            None,
            timeout,
            retries,
            retry_delay,
            Some(payload.to_string()),
        );
        parse_plugin_result(step, result)
    }
}

async fn run_plugin_async(
    step: &PipelineStep,
    pipeline: &PipelineDefinition,
    ctx: &ExecutionContext,
) -> StepResult {
    let repo_root = match RepositoryResolver::new(&ctx.root, pipeline).resolve(&step.repo) {
        Ok(path) => path,
        Err(error) => return error_result_from_config(step, &error),
    };
    let argv = argv_from_step(step);
    let timeout = u64_field(step, "timeout");
    let retries = u64_field(step, "retries").unwrap_or(0);
    let retry_delay = u64_field(step, "retry_delay").unwrap_or(0);
    let payload = serde_json::json!({
        "protocol": "pipecraft.plugin/v1",
        "run_id": ctx.run_id,
        "pipeline": pipeline.name,
        "step_id": step.id,
        "step_type": step.step_type,
        "root": ctx.root.display().to_string(),
        "cwd": repo_root.display().to_string(),
        "labels": ctx.labels,
        "dry_run": ctx.dry_run,
        "with": serde_json::to_value(&step.with).unwrap_or_default(),
        "context": serde_json::to_value(&ctx.context).unwrap_or_default(),
        "artifacts_dir": ctx.artifacts_dir.display().to_string(),
        "logs_dir": ctx.logs_dir.display().to_string(),
    });
    if ctx.dry_run {
        let display = argv.clone().unwrap_or_default().join(" ");
        return StepResult::new(&step.id, &step.step_type, true, status::DRY_RUN, "plugin not executed")
            .with_output(format!("cwd: {}\nplugin: {display}\nprotocol: pipecraft.plugin/v1", repo_root.display()))
            .with_data(serde_json::json!({ "payload_preview": payload, "process_runtime": "tokio" }));
    }
    let result = run_process_with_retries_async(
        step,
        pipeline,
        ctx,
        &repo_root,
        argv,
        None,
        timeout,
        retries,
        retry_delay,
        Some(payload.to_string()),
    )
    .await;
    parse_plugin_result(step, result)
}

fn parse_plugin_result(step: &PipelineStep, result: StepResult) -> StepResult {
    let process_success = result.success;
    let process_data = result.data.clone();
    let stdout_owned = result.data.get("stdout").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let Ok(json) = serde_json::from_str::<serde_json::Value>(stdout_owned.trim()) else {
        return result;
    };
    let declared_success = json.get("success").and_then(|v| v.as_bool()).unwrap_or(process_success);
    // A non-zero process exit always remains a failure, but a structured plugin
    // failure is still parsed so its message/error data are not discarded.
    let success = process_success && declared_success;
    let status_value = json.get("status").and_then(|v| v.as_str()).unwrap_or(if success { status::OK } else { status::FAILED });
    let message = json.get("message").and_then(|v| v.as_str()).unwrap_or("plugin completed");
    let output = json.get("output").and_then(|v| v.as_str()).unwrap_or("");
    let mut data = json.get("data").cloned().unwrap_or_else(|| serde_json::json!({}));
    if let Some(map) = data.as_object_mut() {
        map.insert("process".into(), process_data);
    }
    StepResult::new(&step.id, &step.step_type, success, status_value, message)
        .with_output(output)
        .with_data(data)
}

// ── file_check / boundary_check ─────────────────────────────────────────────

pub struct FileCheckExecutor;
impl StepExecutor for FileCheckExecutor {
    fn step_type(&self) -> &'static str {
        "file_check"
    }
    fn run(&self, step: &PipelineStep, pipeline: &PipelineDefinition, ctx: &ExecutionContext) -> StepResult {
        run_forbidden_scan(step, pipeline, ctx, "file_check")
    }
}

pub struct BoundaryCheckExecutor;
impl StepExecutor for BoundaryCheckExecutor {
    fn step_type(&self) -> &'static str {
        "boundary_check"
    }
    fn run(&self, step: &PipelineStep, pipeline: &PipelineDefinition, ctx: &ExecutionContext) -> StepResult {
        run_forbidden_scan(step, pipeline, ctx, "boundary_check")
    }
}

fn run_forbidden_scan(step: &PipelineStep, pipeline: &PipelineDefinition, ctx: &ExecutionContext, prefix: &str) -> StepResult {
    let repo_root = match RepositoryResolver::new(&ctx.root, pipeline).resolve(&step.repo) {
        Ok(p) => p,
        Err(e) => return error_result_from_config(step, &e),
    };
    if !repo_root.exists() {
        return error_result(
            step,
            format!("repo path does not exist: {}", repo_root.display()),
            "REPO_PATH_MISSING",
            "Create the repo path or correct `repos.<id>.path`.",
        );
    }
    let findings = scan_forbidden(&repo_root, &step.rules, prefix);
    let ok = findings.is_empty();
    let artifact = write_artifact(ctx, &step.id, "findings.txt", &findings.join("\n"));
    let mut data = serde_json::json!({
        "findings_count": findings.len(),
        "repo_root": repo_root.display().to_string(),
    });
    if let Some(path) = artifact {
        data["artifact"] = serde_json::json!(path);
    }
    StepResult::new(
        &step.id,
        &step.step_type,
        ok,
        if ok { status::OK } else { status::FAILED },
        if ok { "boundary/file check passed".into() } else { format!("{} findings", findings.len()) },
    )
    .with_output(findings.join("\n"))
    .with_data(data)
}

// ── git_diff ────────────────────────────────────────────────────────────────

pub struct GitDiffExecutor;
impl StepExecutor for GitDiffExecutor {
    fn step_type(&self) -> &'static str {
        "git_diff"
    }
    fn run(&self, step: &PipelineStep, pipeline: &PipelineDefinition, ctx: &ExecutionContext) -> StepResult {
        let repo_root = match RepositoryResolver::new(&ctx.root, pipeline).resolve(&step.repo) {
            Ok(p) => p,
            Err(e) => return error_result_from_config(step, &e),
        };
        let mode = step.with.get("mode").and_then(yamlx::as_string).unwrap_or_else(|| "status".into());
        let argv = git_diff_argv(&mode);
        // `git_diff` is read-only, so it is allowed even in dry-run mode.
        run_process_with_retries(step, pipeline, ctx, &repo_root, Some(argv), None, u64_field(step, "timeout"), 0, 0, None)
    }

    fn run_async<'a>(
        &'a self,
        step: &'a PipelineStep,
        pipeline: &'a PipelineDefinition,
        ctx: &'a ExecutionContext,
    ) -> StepFuture<'a> {
        Box::pin(run_git_diff_async(step, pipeline, ctx))
    }
}

fn git_diff_argv(mode: &str) -> Vec<String> {
    match mode {
        "stat" => vec!["git".into(), "diff".into(), "--stat".into()],
        "name-only" => vec!["git".into(), "diff".into(), "--name-only".into()],
        "cached" => vec!["git".into(), "diff".into(), "--cached".into(), "--stat".into()],
        _ => vec!["git".into(), "status".into(), "--short".into()],
    }
}

async fn run_git_diff_async(
    step: &PipelineStep,
    pipeline: &PipelineDefinition,
    ctx: &ExecutionContext,
) -> StepResult {
    let repo_root = match RepositoryResolver::new(&ctx.root, pipeline).resolve(&step.repo) {
        Ok(p) => p,
        Err(e) => return error_result_from_config(step, &e),
    };
    let mode = step
        .with
        .get("mode")
        .and_then(yamlx::as_string)
        .unwrap_or_else(|| "status".into());
    run_process_with_retries_async(
        step,
        pipeline,
        ctx,
        &repo_root,
        Some(git_diff_argv(&mode)),
        None,
        u64_field(step, "timeout"),
        0,
        0,
        None,
    )
    .await
}

// ── copy_or_sync ────────────────────────────────────────────────────────────

pub struct CopyOrSyncExecutor;
impl StepExecutor for CopyOrSyncExecutor {
    fn step_type(&self) -> &'static str {
        "copy_or_sync"
    }
    fn run(&self, step: &PipelineStep, pipeline: &PipelineDefinition, ctx: &ExecutionContext) -> StepResult {
        if step.requires_approval && !ctx.approve {
            return StepResult::new(&step.id, &step.step_type, false, status::NEEDS_APPROVAL, "copy/sync requires approval");
        }
        let repo_root = match RepositoryResolver::new(&ctx.root, pipeline).resolve(&step.repo) {
            Ok(p) => p,
            Err(e) => return error_result_from_config(step, &e),
        };
        let source = step.with.get("source").and_then(yamlx::as_string).unwrap_or_else(|| ".".into());
        let dest = match step.with.get("destination").and_then(yamlx::as_string) {
            Some(s) if !s.trim().is_empty() => s,
            _ => return error_result(step, "copy_or_sync requires with.destination", "COPY_CONFIG_ERROR", "Set `with.destination` to a path."),
        };
        let source_abs = resolve_path(&repo_root, &source);
        let dest_abs = resolve_path(&repo_root, &dest);
        let includes = list_field(&step.with, "include");
        let excludes = list_field(&step.with, "exclude");
        let max_files = u64_field(step, "max_files").unwrap_or(10_000) as usize;
        let include_set = build_optional_globset(&includes);
        let exclude_set = build_globset(&excludes);
        if !source_abs.exists() {
            return error_result(step, format!("source path does not exist: {}", source_abs.display()), "COPY_SOURCE_MISSING", "Correct `with.source`.");
        }
        let mut planned = Vec::new();
        for entry in WalkDir::new(&source_abs).into_iter().filter_map(|e| e.ok()) {
            if !entry.file_type().is_file() {
                continue;
            }
            if entry.path().starts_with(&dest_abs) {
                continue;
            }
            let rel = match entry.path().strip_prefix(&source_abs) {
                Ok(r) => r.to_string_lossy().replace('\\', "/"),
                Err(_) => continue,
            };
            if let Some(include) = &include_set {
                if !include.is_match(&rel) {
                    continue;
                }
            }
            if exclude_set.is_match(&rel) {
                continue;
            }
            planned.push(rel);
            if planned.len() >= max_files {
                break;
            }
        }
        if ctx.dry_run {
            return StepResult::new(&step.id, &step.step_type, true, status::DRY_RUN, format!("{} files planned", planned.len()))
                .with_output(planned.join("\n"))
                .with_data(serde_json::json!({
                    "source": source_abs.display().to_string(),
                    "destination": dest_abs.display().to_string(),
                    "planned_files": planned.len(),
                }));
        }
        let mut copied = 0usize;
        for rel in &planned {
            let src = source_abs.join(rel);
            let dst = dest_abs.join(rel);
            if let Some(parent) = dst.parent() {
                if let Err(e) = std::fs::create_dir_all(parent) {
                    return error_result(step, format!("could not create destination dir: {e}"), "COPY_DEST_CREATE_ERROR", "Check destination permissions.");
                }
            }
            if let Err(e) = std::fs::copy(&src, &dst) {
                return error_result(step, format!("could not copy {}: {e}", rel), "COPY_FILE_ERROR", "Check file permissions and destination path.");
            }
            copied += 1;
        }
        let artifact = write_artifact(ctx, &step.id, "copied-files.txt", &planned.join("\n"));
        let mut data = serde_json::json!({
            "source": source_abs.display().to_string(),
            "destination": dest_abs.display().to_string(),
            "copied_files": copied,
        });
        if let Some(path) = artifact {
            data["artifact"] = serde_json::json!(path);
        }
        StepResult::new(&step.id, &step.step_type, true, status::OK, format!("copied {copied} files"))
            .with_output(planned.join("\n"))
            .with_data(data)
    }
}

// ── target_plan ─────────────────────────────────────────────────────────────

pub struct TargetPlanExecutor;
impl StepExecutor for TargetPlanExecutor {
    fn step_type(&self) -> &'static str {
        "target_plan"
    }
    fn run(&self, step: &PipelineStep, pipeline: &PipelineDefinition, ctx: &ExecutionContext) -> StepResult {
        let target_name = resolve_target_name(step, pipeline, ctx).unwrap_or_else(|| "default".into());

        let mut merged = pipeline.target.clone();
        if let Some(serde_yaml::Value::Mapping(named)) = pipeline.targets.get(&target_name) {
            for (k, v) in named {
                if let Some(k) = yamlx::as_string(k) {
                    merged.insert(k, v.clone());
                }
            }
        }

        let mut lines = vec![format!("target: {target_name}")];
        for key in ["repo", "path", "branch", "mode", "visibility", "edition"] {
            if let Some(v) = merged.get(key).and_then(yamlx::as_string) {
                lines.push(format!("{key}: {v}"));
            }
        }
        if merged.is_empty() {
            lines.push("No target metadata configured.".into());
        }
        lines.push("No commit, push, release, or destructive sync is performed by this step.".into());

        StepResult::new(&step.id, &step.step_type, true, status::OK, "target plan generated")
            .with_output(lines.join("\n"))
            .with_data(serde_json::json!({ "target": target_name }))
    }
}

// ── shared helpers ──────────────────────────────────────────────────────────

pub(crate) fn resolve_target_name(
    step: &PipelineStep,
    pipeline: &PipelineDefinition,
    ctx: &ExecutionContext,
) -> Option<String> {
    let pick = |m: &pipecraft_core::yamlx::WithMap, k: &str| m.get(k).and_then(yamlx::as_string);
    step.with
        .get("target")
        .and_then(yamlx::as_string)
        .or_else(|| pick(&pipeline.target, "name"))
        .or_else(|| pick(&ctx.context, "target"))
        .or_else(|| pick(&ctx.context, "edition"))
        .or_else(|| pick(&pipeline.context, "target"))
        .or_else(|| pick(&pipeline.context, "edition"))
        .filter(|s| !s.is_empty())
}

fn argv_from_step(step: &PipelineStep) -> Option<Vec<String>> {
    step.with
        .get("argv")
        .and_then(|v| v.as_sequence())
        .map(|seq| seq.iter().filter_map(yamlx::as_string).collect::<Vec<_>>())
        .filter(|v| !v.is_empty())
}

fn u64_field(step: &PipelineStep, key: &str) -> Option<u64> {
    step.with.get(key).and_then(|v| match v {
        serde_yaml::Value::Number(n) => n.as_u64(),
        serde_yaml::Value::String(s) => s.trim().parse::<u64>().ok(),
        _ => None,
    })
}

fn list_field(map: &pipecraft_core::yamlx::WithMap, key: &str) -> Vec<String> {
    map.get(key).and_then(|v| yamlx::as_string_list(v, key).ok()).unwrap_or_default()
}

fn build_globset(patterns: &[String]) -> GlobSet {
    let mut builder = GlobSetBuilder::new();
    for p in patterns {
        if let Ok(g) = Glob::new(p) {
            builder.add(g);
        }
    }
    builder.build().unwrap_or_else(|_| GlobSetBuilder::new().build().unwrap())
}

fn build_optional_globset(patterns: &[String]) -> Option<GlobSet> {
    if patterns.is_empty() { None } else { Some(build_globset(patterns)) }
}

fn resolve_path(base: &Path, value: &str) -> PathBuf {
    let p = PathBuf::from(value);
    if p.is_absolute() { p } else { base.join(p) }
}

fn safe_name(s: &str) -> String {
    s.chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.' { c } else { '_' })
        .collect()
}

fn write_artifact(ctx: &ExecutionContext, step_id: &str, filename: &str, content: &str) -> Option<String> {
    let dir = ctx.artifacts_dir.join(safe_name(step_id));
    std::fs::create_dir_all(&dir).ok()?;
    let path = dir.join(filename);
    std::fs::write(&path, content).ok()?;
    Some(path.display().to_string())
}

fn write_log(ctx: &ExecutionContext, step_id: &str, name: &str, content: &[u8]) -> Option<String> {
    let dir = ctx.logs_dir.join(safe_name(step_id));
    std::fs::create_dir_all(&dir).ok()?;
    let path = dir.join(name);
    std::fs::write(&path, content).ok()?;
    Some(path.display().to_string())
}

fn build_command(
    argv: Option<Vec<String>>,
    shell_command: Option<String>,
    cwd: &Path,
    pipeline: &PipelineDefinition,
    step: &PipelineStep,
    ctx: &ExecutionContext,
    stdin_payload: Option<String>,
) -> Result<Command, StepResult> {
    let mut cmd = if let Some(parts) = argv {
        if parts.is_empty() {
            return Err(error_result(step, "argv cannot be empty", "COMMAND_CONFIG_ERROR", "Set with.argv to a non-empty list."));
        }
        let mut c = Command::new(&parts[0]);
        c.args(&parts[1..]);
        c
    } else if let Some(display) = shell_command {
        let mut c = Command::new("sh");
        c.arg("-c").arg(display);
        c
    } else {
        return Err(error_result(step, "missing argv/command", "COMMAND_CONFIG_ERROR", "Set with.argv or with.command."));
    };
    cmd.current_dir(cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if stdin_payload.is_some() {
        cmd.stdin(Stdio::piped());
    }
    if let Some(serde_yaml::Value::Mapping(env)) = step.with.get("env") {
        for (k, v) in env {
            if let (Some(k), Some(v)) = (yamlx::as_string(k), yamlx::as_string(v)) {
                cmd.env(k, v);
            }
        }
    }
    cmd.env("PIPECRAFT_STEP_ID", &step.id)
        .env("PIPECRAFT_PIPELINE", &pipeline.name)
        .env("PIPECRAFT_RUN_ID", &ctx.run_id)
        .env("PIPECRAFT_LABELS", ctx.labels.join(" "))
        .env("PIPECRAFT_DRY_RUN", if ctx.dry_run { "true" } else { "false" })
        .env("PIPECRAFT_RUN_DIR", ctx.run_dir.display().to_string())
        .env("PIPECRAFT_ARTIFACTS_DIR", ctx.artifacts_dir.display().to_string())
        .env("PIPECRAFT_LOGS_DIR", ctx.logs_dir.display().to_string());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    Ok(cmd)
}

#[allow(clippy::too_many_arguments)]
fn run_process_with_retries(
    step: &PipelineStep,
    pipeline: &PipelineDefinition,
    ctx: &ExecutionContext,
    cwd: &Path,
    argv: Option<Vec<String>>,
    shell_command: Option<String>,
    timeout: Option<u64>,
    retries: u64,
    retry_delay: u64,
    stdin_payload: Option<String>,
) -> StepResult {
    let attempts = retries + 1;
    let start_all = Instant::now();
    let display = argv.clone().map(|a| a.join(" ")).or(shell_command.clone()).unwrap_or_default();
    let mut last: Option<StepResult> = None;

    for attempt in 1..=attempts {
        let mut cmd = match build_command(
            argv.clone(),
            shell_command.clone(),
            cwd,
            pipeline,
            step,
            ctx,
            stdin_payload.clone(),
        ) {
            Ok(c) => c,
            Err(r) => return r,
        };
        let started = Instant::now();
        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                return error_result(
                    step,
                    format!("command could not start: {e}"),
                    "COMMAND_START_ERROR",
                    "Check the command, permissions, and working directory.",
                )
            }
        };
        if let Some(payload) = &stdin_payload {
            if let Some(mut stdin) = child.stdin.take() {
                let _ = stdin.write_all(payload.as_bytes());
            }
        }
        let inactivity_timeout = u64_field(step, "inactivity_timeout");
        let waited = wait_with_timeout(child, timeout, inactivity_timeout, ctx, step);
        let duration_ms = started.elapsed().as_millis() as u64;
        let result = match waited {
            Ok((out, timed_out, cancelled)) => process_output(step, ctx, cwd, &display, out, timed_out, cancelled, attempt, attempts, duration_ms, start_all.elapsed().as_millis() as u64),
            Err(e) => error_result(step, format!("command wait failed: {e}"), "COMMAND_WAIT_ERROR", "Keep the report and inspect OS-level process limits."),
        };
        let ok = result.success;
        let was_cancelled = result.status == status::CANCELLED || ctx.cancellation.is_cancelled();
        last = Some(result);
        if ok || was_cancelled || attempt == attempts {
            break;
        }
        if retry_delay > 0 {
            // Keep retries cancellable rather than sleeping for the entire
            // delay after the caller has requested shutdown.
            let until = Instant::now() + Duration::from_secs(retry_delay);
            while Instant::now() < until && !ctx.cancellation.is_cancelled() {
                std::thread::sleep(Duration::from_millis(25));
            }
            if ctx.cancellation.is_cancelled() { break; }
        }
    }
    last.unwrap_or_else(|| error_result(step, "command did not run", "COMMAND_INTERNAL_ERROR", "This is a PipeCraft bug."))
}

fn wait_with_timeout(
    mut child: std::process::Child,
    timeout: Option<u64>,
    inactivity_timeout: Option<u64>,
    ctx: &ExecutionContext,
    step: &PipelineStep,
) -> std::io::Result<(Output, bool, bool)> {
    enum StreamMessage {
        Chunk(&'static str, Vec<u8>),
        Done,
    }

    fn reader_thread<R: Read + Send + 'static>(reader: R, stream: &'static str, tx: std::sync::mpsc::Sender<StreamMessage>) {
        std::thread::spawn(move || {
            let mut reader = BufReader::new(reader);
            loop {
                let mut bytes = Vec::new();
                match reader.read_until(b'\n', &mut bytes) {
                    Ok(0) => break,
                    Ok(_) => { let _ = tx.send(StreamMessage::Chunk(stream, bytes)); }
                    Err(_) => break,
                }
            }
            let _ = tx.send(StreamMessage::Done);
        });
    }

    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let (tx, rx) = std::sync::mpsc::channel();
    let mut readers = 0usize;
    if let Some(out) = stdout { readers += 1; reader_thread(out, "stdout", tx.clone()); }
    if let Some(err) = stderr { readers += 1; reader_thread(err, "stderr", tx.clone()); }
    drop(tx);

    let started = Instant::now();
    let mut last_activity = Instant::now();
    let mut stdout_bytes = Vec::new();
    let mut stderr_bytes = Vec::new();
    let mut done_readers = 0usize;
    let mut exit_status: Option<ExitStatus> = None;
    let mut timed_out = false;
    let mut cancelled = false;
    let mut last_heartbeat = Instant::now();

    loop {
        while let Ok(message) = rx.try_recv() {
            match message {
                StreamMessage::Chunk(stream, bytes) => {
                    last_activity = Instant::now();
                    if stream == "stdout" { stdout_bytes.extend_from_slice(&bytes); } else { stderr_bytes.extend_from_slice(&bytes); }
                    let text = String::from_utf8_lossy(&bytes).into_owned();
                    let _ = emit_event(ctx, "process_output", Some(&step.id), serde_json::json!({
                        "stream": stream,
                        "text": text,
                    }));
                }
                StreamMessage::Done => done_readers += 1,
            }
        }

        if exit_status.is_none() {
            exit_status = child.try_wait()?;
        }
        if exit_status.is_none() && last_heartbeat.elapsed() >= Duration::from_secs(5) {
            let _ = emit_event(ctx, "process_heartbeat", Some(&step.id), serde_json::json!({
                "elapsed_ms": started.elapsed().as_millis() as u64,
                "inactive_ms": last_activity.elapsed().as_millis() as u64,
            }));
            last_heartbeat = Instant::now();
        }
        if exit_status.is_some() && done_readers >= readers {
            break;
        }

        if exit_status.is_none() && ctx.cancellation.is_cancelled() {
            cancelled = true;
            let _ = emit_event(ctx, "process_cancelled", Some(&step.id), serde_json::json!({}));
            exit_status = Some(terminate_process_group(&mut child)?);
        }

        let total_expired = timeout.map(|seconds| started.elapsed() >= Duration::from_secs(seconds)).unwrap_or(false);
        let inactive_expired = inactivity_timeout.map(|seconds| last_activity.elapsed() >= Duration::from_secs(seconds)).unwrap_or(false);
        if exit_status.is_none() && (total_expired || inactive_expired) {
            timed_out = true;
            let reason = if inactive_expired { "inactivity_timeout" } else { "timeout" };
            let _ = emit_event(ctx, "process_timeout", Some(&step.id), serde_json::json!({
                "reason": reason,
                "timeout": timeout,
                "inactivity_timeout": inactivity_timeout,
            }));
            exit_status = Some(terminate_process_group(&mut child)?);
        }

        std::thread::sleep(Duration::from_millis(25));
    }

    // Drain messages queued between the final check and reader termination.
    while let Ok(message) = rx.try_recv() {
        if let StreamMessage::Chunk(stream, bytes) = message {
            if stream == "stdout" { stdout_bytes.extend_from_slice(&bytes); } else { stderr_bytes.extend_from_slice(&bytes); }
        }
    }

    let status = match exit_status { Some(s) => s, None => child.wait()? };
    Ok((Output { status, stdout: stdout_bytes, stderr: stderr_bytes }, timed_out, cancelled))
}

fn terminate_process_group(child: &mut std::process::Child) -> std::io::Result<ExitStatus> {
    #[cfg(unix)]
    {
        let pgid = child.id() as i32;
        unsafe { libc::kill(-pgid, libc::SIGTERM); }
        let deadline = Instant::now() + Duration::from_millis(500);
        while Instant::now() < deadline {
            if let Some(status) = child.try_wait()? {
                return Ok(status);
            }
            std::thread::sleep(Duration::from_millis(25));
        }
        unsafe { libc::kill(-pgid, libc::SIGKILL); }
        return child.wait();
    }
    #[cfg(not(unix))]
    {
        let _ = child.kill();
        child.wait()
    }
}

#[allow(clippy::too_many_arguments)]
fn process_output(
    step: &PipelineStep,
    ctx: &ExecutionContext,
    cwd: &Path,
    display: &str,
    out: Output,
    timed_out: bool,
    cancelled: bool,
    attempt: u64,
    attempts: u64,
    duration_ms: u64,
    total_duration_ms: u64,
) -> StepResult {
    let code = out.status.code().unwrap_or(-1);
    let ok = out.status.success() && !timed_out && !cancelled;
    let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&out.stderr).into_owned();
    let mut combined = stdout.clone();
    if !stderr.is_empty() {
        if !combined.is_empty() {
            combined.push('\n');
        }
        combined.push_str(&stderr);
    }
    let stdout_log = write_log(ctx, &step.id, &format!("attempt-{attempt}-stdout.log"), &out.stdout);
    let stderr_log = write_log(ctx, &step.id, &format!("attempt-{attempt}-stderr.log"), &out.stderr);
    let status_value = if cancelled { status::CANCELLED } else if timed_out { status::TIMEOUT } else if ok { status::OK } else { status::FAILED };
    let message = if cancelled {
        format!("cancelled during attempt {attempt}/{attempts}")
    } else if timed_out {
        format!("timeout after attempt {attempt}/{attempts}")
    } else {
        format!("exit={code} attempt={attempt}/{attempts}")
    };
    let mut result = StepResult::new(&step.id, &step.step_type, ok, status_value, message).with_output(combined);
    result.set_data("returncode", serde_json::json!(code));
    result.set_data("cwd", serde_json::json!(cwd.display().to_string()));
    result.set_data("command", serde_json::json!(display));
    result.set_data("attempt", serde_json::json!(attempt));
    result.set_data("attempts", serde_json::json!(attempts));
    result.set_data("duration_ms", serde_json::json!(duration_ms));
    result.set_data("total_duration_ms", serde_json::json!(total_duration_ms));
    result.set_data("timed_out", serde_json::json!(timed_out));
    result.set_data("cancelled", serde_json::json!(cancelled));
    result.set_data("stdout", serde_json::json!(stdout));
    result.set_data("stderr", serde_json::json!(stderr));
    if let Some(path) = stdout_log {
        result.set_data("stdout_log", serde_json::json!(path));
    }
    if let Some(path) = stderr_log {
        result.set_data("stderr_log", serde_json::json!(path));
    }
    result
}

/// Scan a repo for forbidden paths and forbidden terms declared in a rule map
/// (`forbidden_paths` / `forbidden_files`, `forbidden_terms`, `ignore_paths`,
/// `max_file_bytes`, `max_findings`). Used by `file_check` and `boundary_check`.
pub(crate) fn scan_forbidden(
    repo_root: &Path,
    rules: &pipecraft_core::yamlx::WithMap,
    prefix: &str,
) -> Vec<String> {
    let list = |key: &str| -> Vec<String> {
        rules.get(key).and_then(|v| yamlx::as_string_list(v, key).ok()).unwrap_or_default()
    };
    let mut forbidden_paths = list("forbidden_paths");
    if forbidden_paths.is_empty() {
        forbidden_paths = list("forbidden_files");
    }
    let forbidden_terms = list("forbidden_terms");
    let mut ignore = list("ignore_paths");
    if ignore.is_empty() {
        ignore = DEFAULT_IGNORE.iter().map(|s| s.to_string()).collect();
    }
    let max_bytes = rules
        .get("max_file_bytes")
        .and_then(|v| v.as_u64())
        .unwrap_or(DEFAULT_SCAN_MAX_BYTES);
    let max_findings = rules
        .get("max_findings")
        .and_then(|v| v.as_u64())
        .map(|n| n as usize)
        .unwrap_or(DEFAULT_MAX_FINDINGS);

    let ignore_set = build_globset(&ignore);
    let path_set = build_globset(&forbidden_paths);

    let mut findings = Vec::new();
    for entry in WalkDir::new(repo_root).into_iter().filter_map(|e| e.ok()) {
        if !entry.file_type().is_file() {
            continue;
        }
        let rel = match entry.path().strip_prefix(repo_root) {
            Ok(r) => r.to_string_lossy().replace('\\', "/"),
            Err(_) => continue,
        };
        if ignore_set.is_match(&rel) {
            continue;
        }
        if !forbidden_paths.is_empty() && path_set.is_match(&rel) {
            findings.push(format!("{prefix} forbidden path: {rel}"));
            if findings.len() >= max_findings {
                return findings;
            }
        }
        if !forbidden_terms.is_empty() {
            if let Ok(meta) = entry.metadata() {
                if meta.len() > max_bytes {
                    continue;
                }
            }
            if let Ok(text) = std::fs::read_to_string(entry.path()) {
                for term in &forbidden_terms {
                    if text.contains(term) {
                        findings.push(format!("{prefix} forbidden term '{term}': {rel}"));
                        break;
                    }
                }
                if findings.len() >= max_findings {
                    return findings;
                }
            }
        }
    }
    findings
}
