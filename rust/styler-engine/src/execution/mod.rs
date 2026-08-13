mod actions;
mod events;
mod process;

use self::actions::{
    action_for, action_preview, execute_artifact, execute_backup, execute_verification, execute_managed_remove, expand_home, StepAction,
};
use self::events::{now_ms, EventEmitter};
use self::process::{cancellation_requested, execute_command};
use crate::error::EngineError;
use crate::journal::Journal;
use crate::registry;
use crate::protocol::{
    ExecutionOptions, ExecutionRequest, ExecutionSummary, PlanStep, EXECUTION_CONFIRMATION,
};
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::io::Write;
use std::path::PathBuf;

const EXECUTION_ENV: &str = "STYLER_ENABLE_EXECUTION";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum StepState {
    Completed,
    Failed,
    Skipped,
    Cancelled,
    Unsupported,
}

pub fn execute<W: Write>(
    request: ExecutionRequest,
    writer: W,
) -> Result<ExecutionSummary, EngineError> {
    validate_request(&request)?;
    let started_at_ms = now_ms();
    let run_id = build_run_id();
    let journal_path = resolve_journal_path(&request.options, &run_id);
    let journal = Journal::open(&journal_path)?;
    let mut emitter = EventEmitter::new(
        writer,
        journal,
        run_id.clone(),
        request.options.journal_output,
    );
    let dry_run = request.options.mode == "dry_run";
    let ordered_steps = ordered_steps(&request)?;
    let total_steps = ordered_steps.len();
    let mut states: BTreeMap<String, StepState> = BTreeMap::new();
    let mut completed_steps = 0usize;
    let mut failed_steps = 0usize;
    let mut skipped_steps = 0usize;
    let mut cancelled_steps = 0usize;
    let mut unsupported_steps = 0usize;
    let mut abort = false;
    let journal_path_for_event = emitter.journal_path();

    emitter.emit(
        "run_started",
        "",
        if dry_run {
            "Simulación iniciada; no se modificarán archivos ni paquetes"
        } else {
            "Ejecución transaccional iniciada"
        },
        json!({
            "mode": request.options.mode.as_str(),
            "plan": request.plan.name.as_str(),
            "total_steps": total_steps,
            "journal_path": journal_path_for_event,
            "elevation": request.options.elevation.as_str(),
        }),
    )?;

    for step in &ordered_steps {
        if abort || (!dry_run && dependency_blocked(step, &states)) {
            states.insert(step.id.clone(), StepState::Skipped);
            skipped_steps += 1;
            emitter.emit(
                "step_skipped",
                &step.id,
                "Paso omitido porque una dependencia falló o la ejecución fue detenida",
                json!({"needs": &step.needs}),
            )?;
            continue;
        }

        if cancellation_requested(&request.options) {
            states.insert(step.id.clone(), StepState::Cancelled);
            cancelled_steps += 1;
            emitter.emit(
                "step_cancelled",
                &step.id,
                "Cancelación solicitada antes de iniciar el paso",
                json!({}),
            )?;
            abort = true;
            continue;
        }

        let action = action_for(step, &request.options)?;
        emitter.emit(
            "step_started",
            &step.id,
            &step.description,
            json!({
                "step_type": step.step_type.as_str(),
                "provider": step.provider.as_str(),
                "required": step.required,
                "stage": step.stage.as_str(),
            }),
        )?;

        if dry_run {
            match &action {
                StepAction::Unsupported { reason } => {
                    states.insert(step.id.clone(), StepState::Unsupported);
                    unsupported_steps += 1;
                    emitter.emit(
                        "step_unsupported",
                        &step.id,
                        reason,
                        action_preview(&action),
                    )?;
                }
                _ => {
                    states.insert(step.id.clone(), StepState::Completed);
                    completed_steps += 1;
                    emitter.emit(
                        "step_preview",
                        &step.id,
                        "Acción validada para simulación",
                        action_preview(&action),
                    )?;
                    emitter.emit(
                        "step_simulated",
                        &step.id,
                        "Paso simulado sin efectos en el sistema",
                        json!({}),
                    )?;
                }
            }
            continue;
        }

        let outcome_result = match action {
            StepAction::Command(spec) => {
                execute_command(&mut emitter, step, &spec, &request.options)
            }
            StepAction::Backup { target } => execute_backup(&mut emitter, step, &target),
            StepAction::Verify { checks, target } => {
                execute_verification(&mut emitter, step, &checks, &target)
            }
            StepAction::Artifact { spec } => execute_artifact(&mut emitter, step, &spec, &request.options),
            StepAction::ManagedRemove { destination, rollback_path, record_id } => execute_managed_remove(&mut emitter, step, &destination, &rollback_path, &record_id),
            StepAction::Unsupported { reason } => {
                emitter.emit(
                    "step_unsupported",
                    &step.id,
                    &reason,
                    json!({"unsupported": true}),
                )?;
                Ok(StepState::Unsupported)
            }
        };
        let outcome = match outcome_result {
            Ok(outcome) => outcome,
            Err(error) => {
                emitter.emit(
                    "step_failed",
                    &step.id,
                    format!("Fallo interno controlado del paso: {error}"),
                    json!({"error": error.to_string()}),
                )?;
                StepState::Failed
            }
        };

        states.insert(step.id.clone(), outcome);
        match outcome {
            StepState::Completed => {
                completed_steps += 1;
                emitter.emit(
                    "step_completed",
                    &step.id,
                    "Paso completado",
                    json!({}),
                )?;
            }
            StepState::Failed => {
                failed_steps += 1;
                // Un fallo requerido bloquea solamente a sus descendientes del DAG.
                // Las ramas independientes continúan. La opción sólo controla
                // si un fallo opcional debe detener globalmente la ejecución.
                if !step.required && !request.options.continue_on_optional_failure {
                    abort = true;
                }
            }
            StepState::Cancelled => {
                cancelled_steps += 1;
                abort = true;
            }
            StepState::Unsupported => {
                unsupported_steps += 1;
                if !step.required && !request.options.continue_on_optional_failure {
                    abort = true;
                }
            }
            StepState::Skipped => {
                skipped_steps += 1;
            }
        }
    }

    let status = if cancelled_steps > 0 {
        "cancelled"
    } else if failed_steps > 0 {
        "failed"
    } else if unsupported_steps > 0 {
        "blocked"
    } else if dry_run {
        "simulated"
    } else {
        "completed"
    };
    if !dry_run {
        let registry_path = if request.options.registry_path.trim().is_empty() { registry::default_registry_path() } else { PathBuf::from(&request.options.registry_path) };
        if status == "completed" || status == "failed" {
            match registry::register_execution(&request.plan, PathBuf::from(emitter.journal_path()).as_path(), &registry_path, &run_id) {
                Ok(records) => { emitter.emit("registry_updated", "", "Registro de instalaciones actualizado", json!({"registry_path":registry_path.display().to_string(),"records":records.len()}))?; }
                Err(error) => { emitter.emit("registry_update_failed", "", "No se pudo actualizar el registro de instalaciones", json!({"error":error.to_string(),"registry_path":registry_path.display().to_string()}))?; }
            }
            for step in &request.plan.steps {
                if step.step_type.starts_with("uninstall_") || step.step_type == "remove_managed_artifact" {
                    if states.get(&step.id) == Some(&StepState::Completed) {
                        if let Some(record_id)=step.config.get("record_id").and_then(serde_json::Value::as_str) { let _=registry::mark_removed(&registry_path,record_id,&run_id); }
                    }
                }
            }
        }
    }

    let summary = ExecutionSummary {
        run_id,
        status: status.to_string(),
        mode: request.options.mode,
        journal_path: emitter.journal_path(),
        started_at_ms,
        ended_at_ms: now_ms(),
        total_steps,
        completed_steps,
        failed_steps,
        skipped_steps,
        cancelled_steps,
        unsupported_steps,
        dry_run,
    };
    emitter.emit(
        "run_finished",
        "",
        format!("Ejecución terminada con estado {status}"),
        serde_json::to_value(&summary)?,
    )?;
    emitter.finish()?;
    Ok(summary)
}

fn validate_request(request: &ExecutionRequest) -> Result<(), EngineError> {
    if !request.plan.executable {
        return Err(EngineError::InvalidRequest(
            "el plan contiene errores y no está marcado como ejecutable".to_string(),
        ));
    }
    if request.options.mode != "dry_run" && request.options.mode != "apply" {
        return Err(EngineError::InvalidRequest(format!(
            "modo de ejecución desconocido '{}'; usa dry_run o apply",
            request.options.mode
        )));
    }
    if request.options.default_timeout_seconds == 0 {
        return Err(EngineError::InvalidRequest(
            "default_timeout_seconds debe ser mayor que cero".to_string(),
        ));
    }
    if !matches!(
        request.options.elevation.as_str(),
        "none" | "sudo_noninteractive"
    ) {
        return Err(EngineError::InvalidRequest(format!(
            "estrategia de elevación no soportada: {}",
            request.options.elevation
        )));
    }
    validate_environment(&request.options.environment)?;
    if request.options.mode == "apply" {
        if env::var(EXECUTION_ENV).ok().as_deref() != Some("1") {
            return Err(EngineError::InvalidRequest(format!(
                "la ejecución real requiere {EXECUTION_ENV}=1"
            )));
        }
        if request.options.confirmation != EXECUTION_CONFIRMATION {
            return Err(EngineError::InvalidRequest(
                "falta la confirmación explícita para modificar el sistema".to_string(),
            ));
        }
    }
    Ok(())
}

fn validate_environment(values: &BTreeMap<String, String>) -> Result<(), EngineError> {
    let allowed: BTreeSet<&str> = [
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "HOME",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "DEBIAN_FRONTEND",
    ]
    .into_iter()
    .collect();
    for key in values.keys() {
        if !allowed.contains(key.as_str()) {
            return Err(EngineError::InvalidRequest(format!(
                "la variable de entorno '{key}' no está permitida por la política del executor"
            )));
        }
    }
    Ok(())
}

fn ordered_steps(request: &ExecutionRequest) -> Result<Vec<PlanStep>, EngineError> {
    let mut by_id = BTreeMap::new();
    for step in &request.plan.steps {
        if by_id.insert(step.id.clone(), step.clone()).is_some() {
            return Err(EngineError::InvalidRequest(format!(
                "el plan contiene el paso duplicado '{}'",
                step.id
            )));
        }
    }
    if request.plan.order.len() != request.plan.steps.len() {
        return Err(EngineError::InvalidRequest(
            "el orden topológico no contiene exactamente todos los pasos".to_string(),
        ));
    }
    let known_ids: BTreeSet<String> = by_id.keys().cloned().collect();
    let mut seen = BTreeSet::new();
    let mut result = Vec::with_capacity(request.plan.order.len());
    for id in &request.plan.order {
        let step = by_id.remove(id).ok_or_else(|| {
            EngineError::InvalidRequest(format!("el orden referencia el paso desconocido '{id}'"))
        })?;
        for dependency in &step.needs {
            if !known_ids.contains(dependency) {
                return Err(EngineError::InvalidRequest(format!(
                    "el paso '{}' depende del paso inexistente '{}'",
                    step.id, dependency
                )));
            }
            if !seen.contains(dependency) {
                return Err(EngineError::InvalidRequest(format!(
                    "el orden no es topológico: '{}' aparece antes que su dependencia '{}'",
                    step.id, dependency
                )));
            }
        }
        seen.insert(step.id.clone());
        result.push(step);
    }
    if !by_id.is_empty() {
        return Err(EngineError::InvalidRequest(
            "hay pasos que no aparecen en el orden topológico".to_string(),
        ));
    }
    Ok(result)
}

fn dependency_blocked(step: &PlanStep, states: &BTreeMap<String, StepState>) -> bool {
    step.needs.iter().any(|dependency| {
        matches!(
            states.get(dependency),
            Some(StepState::Failed)
                | Some(StepState::Skipped)
                | Some(StepState::Cancelled)
                | Some(StepState::Unsupported)
        )
    })
}

fn resolve_journal_path(options: &ExecutionOptions, run_id: &str) -> PathBuf {
    if !options.journal_path.trim().is_empty() {
        return expand_home(&options.journal_path);
    }
    let base = env::var("XDG_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            env::var("HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|_| PathBuf::from("."))
                .join(".local/state")
        });
    base.join("styler")
        .join("journals")
        .join(format!("{run_id}.jsonl"))
}

fn build_run_id() -> String {
    format!("run-{}-{}", now_ms(), std::process::id())
}

#[cfg(test)]
mod tests {
    use super::{dependency_blocked, ordered_steps, validate_environment, StepState};
    use crate::protocol::{
        ExecutionOptions, ExecutionRequest, PlanResult, PlanStep, TargetRequestOutput,
    };
    use serde_json::Value;
    use std::collections::{BTreeMap, BTreeSet};

    fn step(id: &str, needs: &[&str]) -> PlanStep {
        PlanStep {
            id: id.to_string(),
            step_type: "verify".to_string(),
            description: String::new(),
            needs: needs.iter().map(|value| (*value).to_string()).collect(),
            requires: Vec::new(),
            provides: Vec::new(),
            exclusive_resources: Vec::new(),
            shared_resources: Vec::new(),
            criticality: "normal".to_string(),
            stage: "verify".to_string(),
            required: true,
            provider: String::new(),
            config: Value::Null,
            rollback: Value::Null,
        }
    }

    fn request(order: &[&str], steps: Vec<PlanStep>) -> ExecutionRequest {
        ExecutionRequest {
            plan: PlanResult {
                name: "test".to_string(),
                target: TargetRequestOutput {
                    family: "test".to_string(),
                    architecture: "test".to_string(),
                    home: "/tmp".to_string(),
                },
                selected_components: Vec::new(),
                selected_providers: BTreeMap::new(),
                satisfied_capabilities: Vec::new(),
                missing_capabilities: Vec::new(),
                decisions: Vec::new(),
                order: order.iter().map(|value| (*value).to_string()).collect(),
                steps,
                issues: Vec::new(),
                executable: true,
            },
            options: ExecutionOptions::default(),
        }
    }

    #[test]
    fn dangerous_environment_is_rejected() {
        let mut values = BTreeMap::new();
        values.insert("LD_PRELOAD".to_string(), "/tmp/inject.so".to_string());
        assert!(validate_environment(&values).is_err());
    }

    #[test]
    fn request_cannot_override_program_search_path() {
        let mut values = BTreeMap::new();
        values.insert("PATH".to_string(), "/tmp/untrusted".to_string());
        assert!(validate_environment(&values).is_err());
    }

    #[test]
    fn external_plan_must_keep_dependencies_before_dependants() {
        let request = request(&["child", "root"], vec![step("root", &[]), step("child", &["root"])]);
        assert!(ordered_steps(&request).is_err());
    }

    #[test]
    fn failed_branch_does_not_block_independent_step() {
        let failed = step("failed", &[]);
        let child = step("child", &["failed"]);
        let independent = step("independent", &[]);
        let mut states = BTreeMap::new();
        states.insert(failed.id.clone(), StepState::Failed);
        assert!(dependency_blocked(&child, &states));
        assert!(!dependency_blocked(&independent, &states));
        let seen: BTreeSet<_> = states.keys().cloned().collect();
        assert_eq!(seen, BTreeSet::from(["failed".to_string()]));
    }
}
