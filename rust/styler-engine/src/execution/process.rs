use super::actions::CommandSpec;
use super::events::EventEmitter;
use super::StepState;
use crate::error::EngineError;
use crate::protocol::{ExecutionOptions, PlanStep};
use serde_json::json;
use std::collections::BTreeMap;
use std::env;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::process::CommandExt;

const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(1);
const POLL_INTERVAL: Duration = Duration::from_millis(50);

#[derive(Debug)]
enum PipeMessage {
    Line { stream: &'static str, line: String },
    ReadError { stream: &'static str, message: String },
}

#[derive(Debug)]
struct CommandOutcome {
    status: Option<ExitStatus>,
    timed_out: bool,
    cancelled: bool,
    interactive_prompt: bool,
}

pub(crate) fn execute_command<W: Write>(
    emitter: &mut EventEmitter<W>,
    step: &PlanStep,
    spec: &CommandSpec,
    options: &ExecutionOptions,
) -> Result<StepState, EngineError> {
    let (program, args) = elevated_command(spec, options)?;
    let resolved_program = resolve_system_program(&program).ok_or_else(|| {
        EngineError::InvalidRequest(format!(
            "no se encontró el ejecutable permitido '{program}' en las rutas del sistema"
        ))
    })?;
    let runtime_environment = sanitized_environment(options, spec);
    let program_display = resolved_program.to_string_lossy().to_string();
    let mut command = Command::new(&resolved_program);
    command
        .args(&args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env_clear()
        .envs(&runtime_environment);
    #[cfg(unix)]
    command.process_group(0);

    emitter.emit(
        "command_started",
        &step.id,
        format!("Ejecutando {program_display}"),
        json!({
            "program": &program_display,
            "args": &args,
            "timeout_seconds": spec.timeout_seconds,
            "requires_root": spec.requires_root,
            "environment": &spec.environment,
        }),
    )?;

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            emitter.emit(
                "step_failed",
                &step.id,
                format!("No se pudo iniciar {program_display}: {error}"),
                json!({"program": &program_display, "error": error.to_string()}),
            )?;
            return Ok(StepState::Failed);
        }
    };
    let outcome = monitor_child(
        emitter,
        &step.id,
        &mut child,
        Duration::from_secs(spec.timeout_seconds),
        options,
    )?;
    if outcome.cancelled {
        emitter.emit(
            "step_cancelled",
            &step.id,
            "El proceso fue cancelado y su grupo de procesos fue terminado",
            json!({}),
        )?;
        return Ok(StepState::Cancelled);
    }
    if outcome.interactive_prompt {
        emitter.emit(
            "step_failed",
            &step.id,
            "El comando intentó abrir una pregunta interactiva; se detuvo para evitar un bloqueo invisible",
            json!({"interactive_prompt": true}),
        )?;
        return Ok(StepState::Failed);
    }
    if outcome.timed_out {
        emitter.emit(
            "step_failed",
            &step.id,
            format!("El proceso excedió {} segundos", spec.timeout_seconds),
            json!({"timeout_seconds": spec.timeout_seconds}),
        )?;
        return Ok(StepState::Failed);
    }
    let status = outcome.status.ok_or_else(|| {
        EngineError::InvalidRequest("el proceso terminó sin estado de salida".to_string())
    })?;
    emitter.emit(
        "command_finished",
        &step.id,
        format!("Proceso terminado con {status}"),
        json!({"success": status.success(), "code": status.code()}),
    )?;
    if status.success() {
        Ok(StepState::Completed)
    } else {
        emitter.emit(
            "step_failed",
            &step.id,
            format!("El comando terminó con {status}"),
            json!({"code": status.code()}),
        )?;
        Ok(StepState::Failed)
    }
}

fn sanitized_environment(
    options: &ExecutionOptions,
    spec: &CommandSpec,
) -> BTreeMap<String, String> {
    const SAFE_INHERITED_KEYS: &[&str] = &[
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "XDG_DATA_DIRS",
        "DBUS_SESSION_BUS_ADDRESS",
        "XAUTHORITY",
        "TERM",
    ];
    let mut values = BTreeMap::new();
    values.insert(
        "PATH".to_string(),
        "/usr/sbin:/usr/bin:/sbin:/bin".to_string(),
    );
    for key in SAFE_INHERITED_KEYS {
        if let Ok(value) = env::var(key) {
            values.insert((*key).to_string(), value);
        }
    }
    values.extend(options.environment.clone());
    values.extend(spec.environment.clone());
    values
}

fn resolve_system_program(program: &str) -> Option<PathBuf> {
    let path = Path::new(program);
    if path.is_absolute() {
        return path.is_file().then(|| path.to_path_buf());
    }
    if program.contains('/') {
        return None;
    }
    [
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ]
    .into_iter()
    .map(|directory| Path::new(directory).join(program))
    .find(|candidate| candidate.is_file())
}

fn elevated_command(
    spec: &CommandSpec,
    options: &ExecutionOptions,
) -> Result<(String, Vec<String>), EngineError> {
    if !spec.requires_root || running_as_root() {
        return Ok((spec.program.clone(), spec.args.clone()));
    }
    if options.elevation == "sudo_noninteractive" {
        let mut args = vec!["--non-interactive".to_string(), spec.program.clone()];
        args.extend(spec.args.clone());
        return Ok(("sudo".to_string(), args));
    }
    Err(EngineError::InvalidRequest(format!(
        "{} requiere privilegios; usa elevation=sudo_noninteractive o ejecuta el motor como root",
        spec.program
    )))
}

fn monitor_child<W: Write>(
    emitter: &mut EventEmitter<W>,
    step_id: &str,
    child: &mut Child,
    timeout: Duration,
    options: &ExecutionOptions,
) -> Result<CommandOutcome, EngineError> {
    let (sender, receiver) = mpsc::channel();
    let mut handles = Vec::new();
    if let Some(stdout) = child.stdout.take() {
        handles.push(spawn_pipe_reader(stdout, "stdout", sender.clone()));
    }
    if let Some(stderr) = child.stderr.take() {
        handles.push(spawn_pipe_reader(stderr, "stderr", sender.clone()));
    }
    drop(sender);

    let started = Instant::now();
    let mut last_heartbeat = Instant::now();
    let mut timed_out = false;
    let mut cancelled = false;
    let mut interactive_prompt = false;
    let status = loop {
        if drain_pipe_messages(emitter, step_id, &receiver)? {
            interactive_prompt = true;
            terminate_child_tree(child)?;
            break child.wait().ok();
        }
        if let Some(status) = child.try_wait().map_err(EngineError::io_without_path)? {
            break Some(status);
        }
        if cancellation_requested(options) {
            cancelled = true;
            terminate_child_tree(child)?;
            break child.wait().ok();
        }
        if started.elapsed() >= timeout {
            timed_out = true;
            terminate_child_tree(child)?;
            break child.wait().ok();
        }
        if last_heartbeat.elapsed() >= HEARTBEAT_INTERVAL {
            emitter.emit(
                "heartbeat",
                step_id,
                "Proceso en ejecución",
                json!({"elapsed_ms": started.elapsed().as_millis()}),
            )?;
            last_heartbeat = Instant::now();
        }
        thread::sleep(POLL_INTERVAL);
    };

    // No esperamos indefinidamente a los lectores: un nieto del proceso podría
    // conservar los descriptores abiertos. Soltar los handles desacopla los
    // threads y el receptor se drena durante una ventana acotada.
    drop(handles);
    if drain_remaining_pipe_messages(emitter, step_id, &receiver, Duration::from_millis(250))? {
        interactive_prompt = true;
    }
    Ok(CommandOutcome {
        status,
        timed_out,
        cancelled,
        interactive_prompt,
    })
}

fn spawn_pipe_reader<R: Read + Send + 'static>(
    reader: R,
    stream: &'static str,
    sender: Sender<PipeMessage>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let reader = BufReader::new(reader);
        for line in reader.lines() {
            match line {
                Ok(line) => {
                    if sender.send(PipeMessage::Line { stream, line }).is_err() {
                        break;
                    }
                }
                Err(error) => {
                    let _ = sender.send(PipeMessage::ReadError {
                        stream,
                        message: error.to_string(),
                    });
                    break;
                }
            }
        }
    })
}

fn drain_pipe_messages<W: Write>(
    emitter: &mut EventEmitter<W>,
    step_id: &str,
    receiver: &Receiver<PipeMessage>,
) -> Result<bool, EngineError> {
    let mut interactive_prompt = false;
    while let Ok(message) = receiver.try_recv() {
        interactive_prompt |= handle_pipe_message(emitter, step_id, message)?;
    }
    Ok(interactive_prompt)
}

fn drain_remaining_pipe_messages<W: Write>(
    emitter: &mut EventEmitter<W>,
    step_id: &str,
    receiver: &Receiver<PipeMessage>,
    window: Duration,
) -> Result<bool, EngineError> {
    let deadline = Instant::now() + window;
    let mut interactive_prompt = false;
    while Instant::now() < deadline {
        let remaining = deadline.saturating_duration_since(Instant::now());
        match receiver.recv_timeout(remaining.min(Duration::from_millis(25))) {
            Ok(message) => {
                interactive_prompt |= handle_pipe_message(emitter, step_id, message)?;
            }
            Err(RecvTimeoutError::Disconnected) => break,
            Err(RecvTimeoutError::Timeout) => {}
        }
    }
    Ok(interactive_prompt)
}

fn handle_pipe_message<W: Write>(
    emitter: &mut EventEmitter<W>,
    step_id: &str,
    message: PipeMessage,
) -> Result<bool, EngineError> {
    match message {
        PipeMessage::Line { stream, line } => {
            if looks_like_interactive_prompt(&line) {
                emitter.emit(
                    "interactive_prompt_detected",
                    step_id,
                    "Se detectó una pregunta interactiva",
                    json!({"stream": stream, "line": line}),
                )?;
                Ok(true)
            } else {
                emitter.emit(
                    "command_output",
                    step_id,
                    line.clone(),
                    json!({"stream": stream, "line": line}),
                )?;
                Ok(false)
            }
        }
        PipeMessage::ReadError { stream, message } => {
            emitter.emit(
                "command_output_error",
                step_id,
                message.clone(),
                json!({"stream": stream, "error": message}),
            )?;
            Ok(false)
        }
    }
}

fn terminate_child_tree(child: &mut Child) -> Result<(), EngineError> {
    #[cfg(unix)]
    {
        let process_group = -(child.id() as i32);
        unsafe {
            libc::kill(process_group, libc::SIGTERM);
        }
        let grace = Instant::now();
        while grace.elapsed() < Duration::from_millis(750) {
            if child
                .try_wait()
                .map_err(EngineError::io_without_path)?
                .is_some()
            {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(25));
        }
        unsafe {
            libc::kill(process_group, libc::SIGKILL);
        }
        return Ok(());
    }
    #[cfg(not(unix))]
    {
        child.kill().map_err(EngineError::io_without_path)
    }
}

pub(crate) fn cancellation_requested(options: &ExecutionOptions) -> bool {
    !options.cancel_file.trim().is_empty()
        && expand_home(&options.cancel_file).exists()
}

fn expand_home(value: &str) -> PathBuf {
    if value == "~" {
        return env::var("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from(value));
    }
    if let Some(rest) = value.strip_prefix("~/") {
        return env::var("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("~"))
            .join(rest);
    }
    PathBuf::from(value)
}

fn looks_like_interactive_prompt(line: &str) -> bool {
    let normalized = line.trim().to_ascii_lowercase();
    normalized.ends_with("[y/n]")
        || normalized.ends_with("[y/n]:")
        || normalized.ends_with("(y/n)")
        || normalized.ends_with("(y/n):")
        || normalized.ends_with("password:")
        || normalized.contains("enter your password")
        || normalized.contains("press enter to continue")
}

fn running_as_root() -> bool {
    #[cfg(unix)]
    {
        unsafe { libc::geteuid() == 0 }
    }
    #[cfg(not(unix))]
    {
        false
    }
}

#[cfg(test)]
mod tests {
    use super::{looks_like_interactive_prompt, resolve_system_program, sanitized_environment};
    use crate::execution::actions::CommandSpec;
    use crate::protocol::ExecutionOptions;
    use std::collections::BTreeMap;

    #[test]
    fn detects_common_interactive_prompts() {
        assert!(looks_like_interactive_prompt("Continue? [Y/N]"));
        assert!(looks_like_interactive_prompt("Password:"));
        assert!(!looks_like_interactive_prompt("Downloading package metadata"));
    }
    #[test]
    fn command_environment_does_not_inherit_loader_injection() {
        let spec = CommandSpec {
            program: "sh".to_string(),
            args: Vec::new(),
            requires_root: false,
            timeout_seconds: 1,
            environment: BTreeMap::new(),
        };
        let environment = sanitized_environment(&ExecutionOptions::default(), &spec);
        assert!(!environment.contains_key("LD_PRELOAD"));
        assert_eq!(
            environment.get("PATH").map(String::as_str),
            Some("/usr/sbin:/usr/bin:/sbin:/bin")
        );
    }

    #[test]
    fn resolves_only_absolute_or_known_system_programs() {
        assert!(resolve_system_program("../tmp/fake").is_none());
        assert!(resolve_system_program("/bin/sh").is_some());
    }
}
