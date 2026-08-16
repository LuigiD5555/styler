//! Tokio-based subprocess runtime.
//!
//! Process I/O is asynchronous: one runtime can supervise many child processes
//! without dedicating a reader thread to each stdout/stderr pipe. Output uses a
//! bounded channel for backpressure, is streamed to durable per-attempt logs,
//! and only a bounded preview is retained in memory for the final report.

use std::path::Path;
use std::process::{ExitStatus, Stdio};
use std::time::Duration;

use pipecraft_core::model::{PipelineDefinition, PipelineStep};
use pipecraft_core::yamlx;
use pipecraft_report::{status, StepResult};
use tokio::fs::File;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, Command};
use tokio::sync::mpsc;
use tokio::time::{Instant, MissedTickBehavior};

use crate::context::ExecutionContext;
use crate::events::emit_event;
use crate::executor::error_result;

const STREAM_BUFFER_BYTES: usize = 8192;
const STREAM_CHANNEL_DEPTH: usize = 64;
const REPORT_CAPTURE_BYTES: usize = 1024 * 1024;

fn build_command(
    argv: Option<Vec<String>>,
    shell_command: Option<String>,
    cwd: &Path,
    pipeline: &PipelineDefinition,
    step: &PipelineStep,
    ctx: &ExecutionContext,
    stdin_payload: Option<&str>,
) -> Result<Command, StepResult> {
    let mut cmd = if let Some(parts) = argv {
        if parts.is_empty() {
            return Err(error_result(
                step,
                "argv cannot be empty",
                "COMMAND_CONFIG_ERROR",
                "Set with.argv to a non-empty list.",
            ));
        }
        let mut command = Command::new(&parts[0]);
        command.args(&parts[1..]);
        command
    } else if let Some(display) = shell_command {
        let mut command = Command::new("sh");
        command.arg("-c").arg(display);
        command
    } else {
        return Err(error_result(
            step,
            "missing argv/command",
            "COMMAND_CONFIG_ERROR",
            "Set with.argv or with.command.",
        ));
    };

    cmd.current_dir(cwd).stdout(Stdio::piped()).stderr(Stdio::piped());
    if stdin_payload.is_some() {
        cmd.stdin(Stdio::piped());
    }
    if let Some(serde_yaml::Value::Mapping(env)) = step.with.get("env") {
        for (key, value) in env {
            if let (Some(key), Some(value)) = (yamlx::as_string(key), yamlx::as_string(value)) {
                cmd.env(key, value);
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
        cmd.as_std_mut().process_group(0);
    }

    Ok(cmd)
}

enum StreamMessage {
    Chunk(&'static str, Vec<u8>),
    Done,
}

async fn read_stream<R>(mut reader: R, stream: &'static str, tx: mpsc::Sender<StreamMessage>)
where
    R: AsyncRead + Unpin + Send + 'static,
{
    let mut buffer = vec![0_u8; STREAM_BUFFER_BYTES];
    loop {
        match reader.read(&mut buffer).await {
            Ok(0) => break,
            Ok(n) => {
                if tx
                    .send(StreamMessage::Chunk(stream, buffer[..n].to_vec()))
                    .await
                    .is_err()
                {
                    break;
                }
            }
            Err(_) => break,
        }
    }
    let _ = tx.send(StreamMessage::Done).await;
}

async fn terminate_process_group(child: &mut Child) -> std::io::Result<ExitStatus> {
    #[cfg(unix)]
    {
        if let Some(id) = child.id() {
            let pgid = id as i32;
            unsafe {
                libc::kill(-pgid, libc::SIGTERM);
            }
            let deadline = Instant::now() + Duration::from_millis(500);
            while Instant::now() < deadline {
                if let Some(status) = child.try_wait()? {
                    return Ok(status);
                }
                tokio::time::sleep(Duration::from_millis(25)).await;
            }
            unsafe {
                libc::kill(-pgid, libc::SIGKILL);
            }
        } else {
            let _ = child.start_kill();
        }
        child.wait().await
    }
    #[cfg(not(unix))]
    {
        let _ = child.start_kill();
        child.wait().await
    }
}

fn safe_name(value: &str) -> String {
    value
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.') { c } else { '_' })
        .collect()
}

async fn open_attempt_log(
    ctx: &ExecutionContext,
    step_id: &str,
    attempt: u64,
    stream: &str,
) -> (Option<File>, Option<String>) {
    let dir = ctx.logs_dir.join(safe_name(step_id));
    if tokio::fs::create_dir_all(&dir).await.is_err() {
        return (None, None);
    }
    let path = dir.join(format!("attempt-{attempt}-{stream}.log"));
    match File::create(&path).await {
        Ok(file) => (Some(file), Some(path.display().to_string())),
        Err(_) => (None, None),
    }
}

fn append_capture(target: &mut Vec<u8>, bytes: &[u8]) -> bool {
    let room = REPORT_CAPTURE_BYTES.saturating_sub(target.len());
    if room > 0 {
        target.extend_from_slice(&bytes[..bytes.len().min(room)]);
    }
    bytes.len() > room
}

struct WaitOutcome {
    status: ExitStatus,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
    stdout_log: Option<String>,
    stderr_log: Option<String>,
    stdout_truncated: bool,
    stderr_truncated: bool,
    timed_out: bool,
    cancelled: bool,
}

async fn wait_with_timeout(
    mut child: Child,
    timeout: Option<u64>,
    inactivity_timeout: Option<u64>,
    ctx: &ExecutionContext,
    step: &PipelineStep,
    attempt: u64,
) -> std::io::Result<WaitOutcome> {
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let (tx, mut rx) = mpsc::channel(STREAM_CHANNEL_DEPTH);
    let mut readers = 0_usize;
    if let Some(stdout) = stdout {
        readers += 1;
        tokio::spawn(read_stream(stdout, "stdout", tx.clone()));
    }
    if let Some(stderr) = stderr {
        readers += 1;
        tokio::spawn(read_stream(stderr, "stderr", tx.clone()));
    }
    drop(tx);

    let (mut stdout_file, stdout_log) = open_attempt_log(ctx, &step.id, attempt, "stdout").await;
    let (mut stderr_file, stderr_log) = open_attempt_log(ctx, &step.id, attempt, "stderr").await;

    let started = Instant::now();
    let mut last_activity = Instant::now();
    let mut stdout_bytes = Vec::new();
    let mut stderr_bytes = Vec::new();
    let mut stdout_truncated = false;
    let mut stderr_truncated = false;
    let mut done_readers = 0_usize;
    let mut exit_status: Option<ExitStatus> = None;
    let mut timed_out = false;
    let mut cancelled = false;

    let mut tick = tokio::time::interval(Duration::from_millis(25));
    tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
    let mut heartbeat = tokio::time::interval(Duration::from_secs(5));
    heartbeat.set_missed_tick_behavior(MissedTickBehavior::Skip);
    heartbeat.tick().await;

    loop {
        if exit_status.is_some() && done_readers >= readers {
            break;
        }

        tokio::select! {
            _ = ctx.cancellation.cancelled(), if exit_status.is_none() => {
                cancelled = true;
                let _ = emit_event(ctx, "process_cancelled", Some(&step.id), serde_json::json!({}));
                exit_status = Some(terminate_process_group(&mut child).await?);
            }
            message = rx.recv(), if done_readers < readers => {
                match message {
                    Some(StreamMessage::Chunk(stream, bytes)) => {
                        last_activity = Instant::now();
                        if stream == "stdout" {
                            if let Some(file) = stdout_file.as_mut() {
                                let _ = file.write_all(&bytes).await;
                            }
                            stdout_truncated |= append_capture(&mut stdout_bytes, &bytes);
                        } else {
                            if let Some(file) = stderr_file.as_mut() {
                                let _ = file.write_all(&bytes).await;
                            }
                            stderr_truncated |= append_capture(&mut stderr_bytes, &bytes);
                        }
                        let text = String::from_utf8_lossy(&bytes).into_owned();
                        let _ = emit_event(ctx, "process_output", Some(&step.id), serde_json::json!({
                            "stream": stream,
                            "text": text,
                        }));
                    }
                    Some(StreamMessage::Done) => done_readers += 1,
                    None => done_readers = readers,
                }
            }
            _ = heartbeat.tick(), if exit_status.is_none() => {
                let _ = emit_event(ctx, "process_heartbeat", Some(&step.id), serde_json::json!({
                    "elapsed_ms": started.elapsed().as_millis() as u64,
                    "inactive_ms": last_activity.elapsed().as_millis() as u64,
                }));
            }
            _ = tick.tick(), if exit_status.is_none() => {
                if let Some(status) = child.try_wait()? {
                    exit_status = Some(status);
                    continue;
                }
                let total_expired = timeout
                    .map(|seconds| started.elapsed() >= Duration::from_secs(seconds))
                    .unwrap_or(false);
                let inactive_expired = inactivity_timeout
                    .map(|seconds| last_activity.elapsed() >= Duration::from_secs(seconds))
                    .unwrap_or(false);
                if total_expired || inactive_expired {
                    timed_out = true;
                    let reason = if inactive_expired { "inactivity_timeout" } else { "timeout" };
                    let _ = emit_event(ctx, "process_timeout", Some(&step.id), serde_json::json!({
                        "reason": reason,
                        "timeout": timeout,
                        "inactivity_timeout": inactivity_timeout,
                    }));
                    exit_status = Some(terminate_process_group(&mut child).await?);
                }
            }
        }
    }

    while let Ok(message) = rx.try_recv() {
        if let StreamMessage::Chunk(stream, bytes) = message {
            if stream == "stdout" {
                if let Some(file) = stdout_file.as_mut() {
                    let _ = file.write_all(&bytes).await;
                }
                stdout_truncated |= append_capture(&mut stdout_bytes, &bytes);
            } else {
                if let Some(file) = stderr_file.as_mut() {
                    let _ = file.write_all(&bytes).await;
                }
                stderr_truncated |= append_capture(&mut stderr_bytes, &bytes);
            }
        }
    }
    if let Some(file) = stdout_file.as_mut() {
        let _ = file.flush().await;
    }
    if let Some(file) = stderr_file.as_mut() {
        let _ = file.flush().await;
    }

    let status = match exit_status {
        Some(status) => status,
        None => child.wait().await?,
    };
    Ok(WaitOutcome {
        status,
        stdout: stdout_bytes,
        stderr: stderr_bytes,
        stdout_log,
        stderr_log,
        stdout_truncated,
        stderr_truncated,
        timed_out,
        cancelled,
    })
}

#[allow(clippy::too_many_arguments)]
fn process_output(
    step: &PipelineStep,
    cwd: &Path,
    display: &str,
    out: WaitOutcome,
    attempt: u64,
    attempts: u64,
    duration_ms: u64,
    total_duration_ms: u64,
) -> StepResult {
    let code = out.status.code().unwrap_or(-1);
    let ok = out.status.success() && !out.timed_out && !out.cancelled;
    let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&out.stderr).into_owned();
    let mut combined = stdout.clone();
    if !stderr.is_empty() {
        if !combined.is_empty() {
            combined.push('\n');
        }
        combined.push_str(&stderr);
    }
    let status_value = if out.cancelled {
        status::CANCELLED
    } else if out.timed_out {
        status::TIMEOUT
    } else if ok {
        status::OK
    } else {
        status::FAILED
    };
    let message = if out.cancelled {
        format!("cancelled during attempt {attempt}/{attempts}")
    } else if out.timed_out {
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
    result.set_data("timed_out", serde_json::json!(out.timed_out));
    result.set_data("cancelled", serde_json::json!(out.cancelled));
    result.set_data("stdout", serde_json::json!(stdout));
    result.set_data("stderr", serde_json::json!(stderr));
    result.set_data("stdout_truncated", serde_json::json!(out.stdout_truncated));
    result.set_data("stderr_truncated", serde_json::json!(out.stderr_truncated));
    result.set_data("capture_limit_bytes", serde_json::json!(REPORT_CAPTURE_BYTES));
    if let Some(path) = out.stdout_log {
        result.set_data("stdout_log", serde_json::json!(path));
    }
    if let Some(path) = out.stderr_log {
        result.set_data("stderr_log", serde_json::json!(path));
    }
    result
}

#[allow(clippy::too_many_arguments)]
pub async fn run_process_with_retries_async(
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
    let display = argv
        .clone()
        .map(|args| args.join(" "))
        .or(shell_command.clone())
        .unwrap_or_default();
    let mut last = None;

    for attempt in 1..=attempts {
        if ctx.cancellation.is_cancelled() {
            return StepResult::new(
                &step.id,
                &step.step_type,
                false,
                status::CANCELLED,
                "cancelled before process start",
            );
        }

        let mut cmd = match build_command(
            argv.clone(),
            shell_command.clone(),
            cwd,
            pipeline,
            step,
            ctx,
            stdin_payload.as_deref(),
        ) {
            Ok(cmd) => cmd,
            Err(result) => return result,
        };
        let started = Instant::now();
        let mut child = match cmd.spawn() {
            Ok(child) => child,
            Err(error) => {
                return error_result(
                    step,
                    format!("command could not start: {error}"),
                    "COMMAND_START_ERROR",
                    "Check the command, permissions, and working directory.",
                )
            }
        };

        if let Some(payload) = &stdin_payload {
            if let Some(mut stdin) = child.stdin.take() {
                let _ = stdin.write_all(payload.as_bytes()).await;
                let _ = stdin.shutdown().await;
            }
        }

        let inactivity_timeout = step.with.get("inactivity_timeout").and_then(|v| v.as_u64());
        let waited = wait_with_timeout(child, timeout, inactivity_timeout, ctx, step, attempt).await;
        let duration_ms = started.elapsed().as_millis() as u64;
        let result = match waited {
            Ok(output) => process_output(
                step,
                cwd,
                &display,
                output,
                attempt,
                attempts,
                duration_ms,
                start_all.elapsed().as_millis() as u64,
            ),
            Err(error) => error_result(
                step,
                format!("command wait failed: {error}"),
                "COMMAND_WAIT_ERROR",
                "Keep the report and inspect OS-level process limits.",
            ),
        };

        let ok = result.success;
        let was_cancelled = result.status == status::CANCELLED || ctx.cancellation.is_cancelled();
        last = Some(result);
        if ok || was_cancelled || attempt == attempts {
            break;
        }
        if retry_delay > 0 {
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_secs(retry_delay)) => {},
                _ = ctx.cancellation.cancelled() => break,
            }
        }
    }

    last.unwrap_or_else(|| {
        error_result(
            step,
            "command did not run",
            "COMMAND_INTERNAL_ERROR",
            "This is a PipeCraft bug.",
        )
    })
}
