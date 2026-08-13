use super::events::EventEmitter;
use crate::artifact::{ArtifactSpec, ArtifactWorkspace};
use crate::error::EngineError;
use crate::protocol::{ExecutionOptions, PlanStep};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub(crate) struct CommandSpec {
    pub(crate) program: String,
    pub(crate) args: Vec<String>,
    pub(crate) requires_root: bool,
    pub(crate) timeout_seconds: u64,
    pub(crate) environment: BTreeMap<String, String>,
}

#[derive(Debug, Clone)]
pub(crate) enum StepAction {
    Command(CommandSpec),
    Backup { target: PathBuf },
    Verify { checks: Vec<String>, target: PathBuf },
    Artifact { spec: ArtifactSpec },
    ManagedRemove { destination: PathBuf, rollback_path: PathBuf, record_id: String },
    Unsupported { reason: String },
}

pub(crate) fn action_for(
    step: &PlanStep,
    options: &ExecutionOptions,
) -> Result<StepAction, EngineError> {
    let timeout_seconds = step
        .config
        .get("timeout_seconds")
        .and_then(Value::as_u64)
        .unwrap_or(options.default_timeout_seconds);
    if timeout_seconds == 0 {
        return Err(EngineError::InvalidRequest(format!(
            "el paso '{}' declara timeout_seconds=0",
            step.id
        )));
    }
    let action = match step.step_type.as_str() {
        "backup_config" => {
            let target = config_string(&step.config, "target");
            if target.is_empty() {
                StepAction::Unsupported {
                    reason: "el respaldo no tiene una ruta target declarada".to_string(),
                }
            } else {
                StepAction::Backup {
                    target: expand_home(&target),
                }
            }
        }
        "verify" => StepAction::Verify {
            checks: step
                .config
                .get("checks")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default(),
            target: expand_home(&config_string(&step.config, "target")),
        },
        "install_apt" => package_command(
            "apt-get",
            &[
                "-o",
                "Dpkg::Use-Pty=0",
                "-o",
                "DPkg::Lock::Timeout=120",
                "install",
                "-y",
            ],
            step,
            timeout_seconds,
            true,
        ),
        "install_pacman" => package_command(
            "pacman",
            &["-S", "--needed", "--noconfirm"],
            step,
            timeout_seconds,
            true,
        ),
        "install_rpm" => package_command("dnf", &["install", "-y"], step, timeout_seconds, true),
        "install_zypper" => package_command(
            "zypper",
            &["--non-interactive", "install"],
            step,
            timeout_seconds,
            true,
        ),
        "install_snap" => package_command("snap", &["install"], step, timeout_seconds, true),
        "install_flatpak" => {
            let application_id = config_string(&step.config, "application_id");
            let scope = config_string(&step.config, "scope");
            if application_id.is_empty() {
                StepAction::Unsupported {
                    reason: "el proveedor Flatpak no declara application_id".to_string(),
                }
            } else if !safe_package_identifier(&application_id) {
                StepAction::Unsupported {
                    reason: format!("application_id Flatpak inseguro o inválido: {application_id}"),
                }
            } else if !matches!(scope.as_str(), "user" | "system") {
                StepAction::Unsupported {
                    reason: "Flatpak requiere declarar scope=user o scope=system antes de ejecutarse"
                        .to_string(),
                }
            } else {
                let scope_arg = if scope == "user" { "--user" } else { "--system" };
                StepAction::Command(CommandSpec {
                    program: "flatpak".to_string(),
                    args: vec![
                        "install".to_string(),
                        scope_arg.to_string(),
                        "--noninteractive".to_string(),
                        "-y".to_string(),
                        application_id,
                    ],
                    requires_root: scope == "system",
                    timeout_seconds,
                    environment: BTreeMap::new(),
                })
            }
        }
        "install_archive" | "overlay_install" | "install_appimage" | "install_file" => {
            match artifact_spec(step) {
                Ok(spec) => StepAction::Artifact { spec },
                Err(reason) => StepAction::Unsupported { reason },
            }
        },
        "uninstall_apt" => uninstall_package_command("apt-get", &["remove", "-y"], step, timeout_seconds, true),
        "uninstall_pacman" => uninstall_package_command("pacman", &["-R", "--noconfirm"], step, timeout_seconds, true),
        "uninstall_dnf" => uninstall_package_command("dnf", &["remove", "-y"], step, timeout_seconds, true),
        "uninstall_zypper" => uninstall_package_command("zypper", &["--non-interactive", "remove"], step, timeout_seconds, true),
        "uninstall_snap" => uninstall_package_command("snap", &["remove"], step, timeout_seconds, true),
        "uninstall_flatpak" => {
            let application_id = config_string(&step.config, "application_id");
            let scope = config_string(&step.config, "scope");
            if !safe_package_identifier(&application_id) || !matches!(scope.as_str(), "user" | "system") {
                StepAction::Unsupported { reason: "registro Flatpak incompleto o inseguro".to_string() }
            } else {
                StepAction::Command(CommandSpec { program:"flatpak".to_string(), args:vec!["uninstall".to_string(), if scope=="user"{"--user".to_string()}else{"--system".to_string()}, "--noninteractive".to_string(), "-y".to_string(), application_id], requires_root:scope=="system", timeout_seconds, environment:BTreeMap::new() })
            }
        },
        "remove_managed_artifact" => {
            let destination=expand_home(&config_string(&step.config,"destination"));
            let rollback_path=expand_home(&config_string(&step.config,"rollback_path"));
            let record_id=config_string(&step.config,"record_id");
            if destination.as_os_str().is_empty() || record_id.is_empty() { StepAction::Unsupported{reason:"recibo de artefacto incompleto".to_string()} }
            else { StepAction::ManagedRemove{destination,rollback_path,record_id} }
        },
        "apply_config" => StepAction::Unsupported {
            reason: "la configuración no declara todavía una receta de archivos aplicable"
                .to_string(),
        },
        other => StepAction::Unsupported {
            reason: format!("el executor no reconoce todavía el tipo de paso '{other}'"),
        },
    };
    Ok(action)
}

fn artifact_spec(step: &PlanStep) -> Result<ArtifactSpec, String> {
    let source = config_string(&step.config, "source");
    let checksum_sha256 = config_string(&step.config, "checksum_sha256");
    let mut artifact_kind = config_string(&step.config, "artifact_kind");
    if artifact_kind.is_empty() {
        artifact_kind = match step.step_type.as_str() {
            "install_appimage" => "appimage",
            "overlay_install" => "overlay_zip",
            "install_archive" => "zip",
            "install_file" => "binary",
            _ => "",
        }.to_string();
    }
    let destination = config_string(&step.config, "destination");
    if source.is_empty() { return Err("el proveedor no declara source".to_string()); }
    if checksum_sha256.len() != 64 || !checksum_sha256.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err("el artefacto requiere checksum_sha256 válido y obligatorio".to_string());
    }
    if destination.is_empty() { return Err("el artefacto requiere destination explícito".to_string()); }
    Ok(ArtifactSpec {
        source,
        checksum_sha256,
        artifact_kind,
        destination,
        file_name: config_string(&step.config, "file_name"),
        strip_components: step.config.get("strip_components").and_then(Value::as_u64).unwrap_or(0) as usize,
        max_size_bytes: step.config.get("max_size_bytes").and_then(Value::as_u64).unwrap_or(0),
        desktop_entry: step.config.get("desktop_entry").and_then(Value::as_bool).unwrap_or(false),
        executable_name: config_string(&step.config, "executable_name"),
    })
}

pub(crate) fn execute_artifact<W: Write>(
    emitter: &mut EventEmitter<W>,
    step: &PlanStep,
    spec: &ArtifactSpec,
    options: &ExecutionOptions,
) -> Result<super::StepState, EngineError> {
    let workspace = ArtifactWorkspace::new()?;
    emitter.emit("artifact_acquire_started", &step.id, "Adquisición segura iniciada", json!({"source": &spec.source, "kind": &spec.artifact_kind}))?;
    let (staged, size, checksum) = workspace.acquire(spec)?;
    emitter.emit("artifact_verified", &step.id, "Artefacto descargado y verificado", json!({"staged_path": staged, "size_bytes": size, "checksum_sha256": checksum}))?;
    if matches!(spec.artifact_kind.as_str(), "deb" | "rpm") {
        let command = if spec.artifact_kind == "deb" {
            CommandSpec { program: "apt-get".to_string(), args: vec!["-o".to_string(), "Dpkg::Use-Pty=0".to_string(), "install".to_string(), "-y".to_string(), staged.display().to_string()], requires_root: true, timeout_seconds: options.default_timeout_seconds, environment: BTreeMap::from([("DEBIAN_FRONTEND".to_string(), "noninteractive".to_string())]) }
        } else {
            CommandSpec { program: "dnf".to_string(), args: vec!["install".to_string(), "-y".to_string(), staged.display().to_string()], requires_root: true, timeout_seconds: options.default_timeout_seconds, environment: BTreeMap::new() }
        };
        emitter.emit("local_package_install_started", &step.id, "Instalando paquete local verificado", action_preview(&StepAction::Command(command.clone())))?;
        return super::process::execute_command(emitter, step, &command, options);
    }
    let rollback_root = emitter.journal_parent().join("rollbacks").join(sanitize_id(emitter.run_id())).join(sanitize_id(&step.id));
    let receipt = workspace.install(spec, &staged, &rollback_root)?;
    emitter.emit("artifact_installed", &step.id, "Artefacto promovido de forma atómica", serde_json::to_value(receipt)?)?;
    Ok(super::StepState::Completed)
}

fn package_command(
    program: &str,
    prefix: &[&str],
    step: &PlanStep,
    timeout_seconds: u64,
    requires_root: bool,
) -> StepAction {
    let package = step
        .config
        .get("packages")
        .and_then(Value::as_array)
        .and_then(|items| items.first())
        .and_then(Value::as_str)
        .unwrap_or_default();
    if package.is_empty() {
        return StepAction::Unsupported {
            reason: format!("{} no declara un paquete principal", step.id),
        };
    }
    if !safe_package_identifier(package) {
        return StepAction::Unsupported {
            reason: format!("{} declara un identificador de paquete inseguro: {package}", step.id),
        };
    }
    let mut args: Vec<String> = prefix.iter().map(|value| (*value).to_string()).collect();
    args.push(package.to_string());
    let mut environment = BTreeMap::new();
    if program == "apt-get" {
        environment.insert("DEBIAN_FRONTEND".to_string(), "noninteractive".to_string());
    }
    StepAction::Command(CommandSpec {
        program: program.to_string(),
        args,
        requires_root,
        timeout_seconds,
        environment,
    })
}

fn uninstall_package_command(program:&str,prefix:&[&str],step:&PlanStep,timeout_seconds:u64,requires_root:bool)->StepAction{
    let package=config_string(&step.config,"package");
    if !safe_package_identifier(&package){return StepAction::Unsupported{reason:"identificador de desinstalación inseguro".to_string()};}
    let mut args:Vec<String>=prefix.iter().map(|v|(*v).to_string()).collect(); args.push(package);
    let mut environment=BTreeMap::new(); if program=="apt-get"{environment.insert("DEBIAN_FRONTEND".to_string(),"noninteractive".to_string());}
    StepAction::Command(CommandSpec{program:program.to_string(),args,requires_root,timeout_seconds,environment})
}

pub(crate) fn execute_managed_remove<W:Write>(emitter:&mut EventEmitter<W>,step:&PlanStep,destination:&Path,rollback_path:&Path,record_id:&str)->Result<super::StepState,EngineError>{
    if !destination.is_absolute(){return Err(EngineError::InvalidRequest("destino administrado no absoluto".to_string()));}
    if destination.exists(){if destination.is_dir(){fs::remove_dir_all(destination).map_err(|e|EngineError::io(destination,e))?;}else{fs::remove_file(destination).map_err(|e|EngineError::io(destination,e))?;}}
    if !rollback_path.as_os_str().is_empty() && rollback_path.exists(){if let Some(parent)=destination.parent(){fs::create_dir_all(parent).map_err(|e|EngineError::io(parent,e))?;}fs::rename(rollback_path,destination).map_err(|e|EngineError::io(destination,e))?;}
    emitter.emit("managed_installation_removed",&step.id,"Instalación administrada retirada usando su recibo",json!({"record_id":record_id,"destination":destination,"restored_previous":!rollback_path.as_os_str().is_empty()}))?;
    Ok(super::StepState::Completed)
}

fn safe_package_identifier(value: &str) -> bool {
    !value.is_empty()
        && !value.starts_with('-')
        && value.chars().all(|character| {
            character.is_ascii_alphanumeric()
                || matches!(character, '.' | '_' | '+' | '-' | ':' | '@')
        })
}

pub(crate) fn action_preview(action: &StepAction) -> Value {
    match action {
        StepAction::Command(spec) => json!({
            "kind": "command",
            "program": &spec.program,
            "args": &spec.args,
            "requires_root": spec.requires_root,
            "timeout_seconds": spec.timeout_seconds,
            "environment": &spec.environment,
        }),
        StepAction::Backup { target } => json!({
            "kind": "backup",
            "target": target,
        }),
        StepAction::Verify { checks, target } => json!({
            "kind": "verify",
            "checks": checks,
            "target": target,
        }),
        StepAction::Artifact { spec } => json!({
            "kind": "artifact",
            "source": &spec.source,
            "checksum_sha256": &spec.checksum_sha256,
            "artifact_kind": &spec.artifact_kind,
            "destination": &spec.destination,
            "strip_components": spec.strip_components,
            "max_size_bytes": spec.max_size_bytes,
        }),
        StepAction::ManagedRemove { destination, rollback_path, record_id } => json!({
            "kind":"managed_remove", "destination":destination, "rollback_path":rollback_path, "record_id":record_id
        }),
        StepAction::Unsupported { reason } => json!({
            "kind": "unsupported",
            "reason": reason,
        }),
    }
}

pub(crate) fn execute_backup<W: Write>(
    emitter: &mut EventEmitter<W>,
    step: &PlanStep,
    target: &Path,
) -> Result<super::StepState, EngineError> {
    if !target.exists() {
        emitter.emit(
            "backup_skipped",
            &step.id,
            "La ruta no existe; no hay configuración previa que respaldar",
            json!({"target": target}),
        )?;
        return Ok(super::StepState::Completed);
    }
    let journal_parent = emitter.journal_parent();
    let source_canonical = target
        .canonicalize()
        .map_err(|error| EngineError::io(target, error))?;
    if let Ok(journal_canonical) = journal_parent.canonicalize() {
        if journal_canonical.starts_with(&source_canonical) {
            return Err(EngineError::InvalidRequest(format!(
                "el journal está dentro de la ruta a respaldar y produciría una copia recursiva: {}",
                journal_canonical.display()
            )));
        }
    }
    let destination = journal_parent
        .join("backups")
        .join(sanitize_id(emitter.run_id()))
        .join(sanitize_id(&step.id));
    if destination.exists() {
        return Err(EngineError::InvalidRequest(format!(
            "el destino de respaldo ya existe: {}",
            destination.display()
        )));
    }
    copy_path(target, &destination)?;
    emitter.emit(
        "backup_created",
        &step.id,
        "Respaldo creado",
        json!({"target": target, "backup": destination}),
    )?;
    Ok(super::StepState::Completed)
}

pub(crate) fn execute_verification<W: Write>(
    emitter: &mut EventEmitter<W>,
    step: &PlanStep,
    checks: &[String],
    target: &Path,
) -> Result<super::StepState, EngineError> {
    if checks.is_empty() {
        emitter.emit(
            "verification_warning",
            &step.id,
            "El paso no declara verificaciones",
            json!({}),
        )?;
        return Ok(super::StepState::Completed);
    }
    let mut all_ok = true;
    for check in checks {
        let (ok, detail) = run_check(check, target);
        all_ok &= ok;
        emitter.emit(
            "verification_result",
            &step.id,
            if ok {
                format!("Verificación aprobada: {check}")
            } else {
                format!("Verificación fallida: {check}")
            },
            json!({"check": check, "ok": ok, "detail": detail}),
        )?;
    }
    if all_ok {
        Ok(super::StepState::Completed)
    } else {
        emitter.emit(
            "step_failed",
            &step.id,
            "Una o más verificaciones fallaron",
            json!({}),
        )?;
        Ok(super::StepState::Failed)
    }
}

fn run_check(check: &str, target: &Path) -> (bool, String) {
    let Some((kind, value)) = check.split_once(':') else {
        return (false, "formato de verificación desconocido".to_string());
    };
    match kind {
        "executable" => {
            let found = find_command(value);
            (
                found.is_some(),
                found
                    .map(|path| path.to_string_lossy().to_string())
                    .unwrap_or_else(|| format!("no se encontró '{value}' en PATH")),
            )
        }
        "directory" => {
            let path = if value.starts_with('/') || value.starts_with('~') {
                expand_home(value)
            } else {
                target.to_path_buf()
            };
            (path.is_dir(), path.to_string_lossy().to_string())
        }
        "file" => {
            let path = if value.starts_with('/') || value.starts_with('~') {
                expand_home(value)
            } else {
                target.join(value)
            };
            (path.is_file(), path.to_string_lossy().to_string())
        }
        "marker" => {
            let path = target.join(".styler-markers").join(value);
            (path.is_file(), path.to_string_lossy().to_string())
        }
        _ => (false, format!("tipo de verificación '{kind}' no soportado")),
    }
}

fn copy_path(source: &Path, destination: &Path) -> Result<(), EngineError> {
    let metadata = fs::symlink_metadata(source).map_err(|error| EngineError::io(source, error))?;
    if metadata.file_type().is_symlink() {
        #[cfg(unix)]
        {
            use std::os::unix::fs::symlink;
            let link = fs::read_link(source).map_err(|error| EngineError::io(source, error))?;
            if let Some(parent) = destination.parent() {
                fs::create_dir_all(parent).map_err(|error| EngineError::io(parent, error))?;
            }
            symlink(link, destination).map_err(|error| EngineError::io(destination, error))?;
            return Ok(());
        }
        #[cfg(not(unix))]
        {
            return Err(EngineError::InvalidRequest(format!(
                "no se puede respaldar el enlace simbólico {} en esta plataforma",
                source.display()
            )));
        }
    }
    if metadata.is_dir() {
        fs::create_dir_all(destination).map_err(|error| EngineError::io(destination, error))?;
        for entry in fs::read_dir(source).map_err(|error| EngineError::io(source, error))? {
            let entry = entry.map_err(EngineError::io_without_path)?;
            copy_path(&entry.path(), &destination.join(entry.file_name()))?;
        }
    } else if metadata.is_file() {
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent).map_err(|error| EngineError::io(parent, error))?;
        }
        fs::copy(source, destination).map_err(|error| EngineError::io(source, error))?;
    }
    Ok(())
}

pub(crate) fn expand_home(value: &str) -> PathBuf {
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

fn config_string(config: &Value, key: &str) -> String {
    config
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

fn sanitize_id(value: &str) -> String {
    value
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | '.') {
                character
            } else {
                '_'
            }
        })
        .collect()
}

fn find_command(name: &str) -> Option<PathBuf> {
    if name.contains('/') {
        let path = PathBuf::from(name);
        return path.is_file().then_some(path);
    }
    env::var_os("PATH").and_then(|path| {
        env::split_paths(&path)
            .map(|directory| directory.join(name))
            .find(|candidate| candidate.is_file())
    })
}

#[cfg(test)]
mod tests {
    use super::{action_for, StepAction};
    use crate::protocol::{ExecutionOptions, PlanStep};
    use serde_json::json;

    fn step(step_type: &str, config: serde_json::Value) -> PlanStep {
        PlanStep {
            id: "install:test".to_string(),
            step_type: step_type.to_string(),
            description: String::new(),
            needs: Vec::new(),
            requires: Vec::new(),
            provides: Vec::new(),
            exclusive_resources: Vec::new(),
            shared_resources: Vec::new(),
            criticality: "normal".to_string(),
            stage: "install".to_string(),
            required: true,
            provider: "apt".to_string(),
            config,
            rollback: serde_json::Value::Null,
        }
    }

    #[test]
    fn package_plan_uses_only_primary_package() {
        let action = action_for(
            &step("install_apt", json!({"packages": ["primary", "alternative"]})),
            &ExecutionOptions::default(),
        )
        .unwrap();
        match action {
            StepAction::Command(spec) => {
                assert_eq!(spec.program, "apt-get");
                assert_eq!(
                    spec.args,
                    vec![
                        "-o".to_string(),
                        "Dpkg::Use-Pty=0".to_string(),
                        "-o".to_string(),
                        "DPkg::Lock::Timeout=120".to_string(),
                        "install".to_string(),
                        "-y".to_string(),
                        "primary".to_string(),
                    ]
                );
                assert_eq!(
                    spec.environment.get("DEBIAN_FRONTEND").map(String::as_str),
                    Some("noninteractive")
                );
            }
            _ => panic!("se esperaba un comando"),
        }
    }

    #[test]
    fn archive_provider_is_explicitly_deferred() {
        let action = action_for(
            &step("install_archive", json!({"source": "https://example.invalid/a.zip"})),
            &ExecutionOptions::default(),
        )
        .unwrap();
        assert!(matches!(action, StepAction::Unsupported { .. }));
    }
    #[test]
    fn package_option_injection_is_rejected() {
        let action = action_for(
            &step("install_apt", json!({"packages": ["--allow-unauthenticated"]})),
            &ExecutionOptions::default(),
        )
        .unwrap();
        assert!(matches!(action, StepAction::Unsupported { .. }));
    }
}
