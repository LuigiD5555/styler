//! Static, pre-execution validation.
//!
//! V1.1 strengthens validation for the practical maintenance use cases PipeCraft
//! targets: command argv must be non-empty, retries/timeouts must be sane, and
//! `on_error.types.<...>` must name a real step type rather than accidentally
//! using a step id.

use std::collections::HashSet;

use crate::model::{PipelineDefinition, PipelineStep};
use crate::yamlx;

const VALID_RISKS: [&str; 3] = ["low", "medium", "high"];
const VALID_ERROR_POLICIES: [&str; 3] = ["stop", "continue", "warn"];
const VALID_RUN_IF: [&str; 4] = ["all_success", "all_complete", "any_failed", "always"];
const VALID_STATUSES: [&str; 14] = [
    "ok",
    "ok_with_warnings",
    "skipped",
    "failed",
    "needs_approval",
    "dry_run",
    "planned",
    "timeout",
    "cancelled",
    "pending",
    "ready",
    "running",
    "succeeded",
    "blocked",
];

/// Validate everything that does not require building the DAG.
/// `known_step_types` is the registry of executor type names.
pub fn validate_static(pipeline: &PipelineDefinition, known_step_types: &HashSet<String>) -> Vec<String> {
    let mut errors = Vec::new();

    if pipeline.name.is_empty() {
        errors.push("pipeline name is required".to_string());
    }
    if pipeline.steps.is_empty() {
        errors.push("pipeline must contain at least one step".to_string());
    }

    let repo_ids: HashSet<&String> = pipeline.repos.keys().collect();
    let target_names: HashSet<&String> = pipeline.targets.keys().collect();
    let step_ids: HashSet<&String> = pipeline.steps.iter().map(|s| &s.id).collect();

    // Primary target sanity (only enforced when the relevant maps are present).
    if let Some(repo) = pipeline.target.get("repo").and_then(yamlx::as_string) {
        if !repo_ids.is_empty() && !repo_ids.contains(&repo) {
            errors.push(format!("target.repo references unknown repo '{repo}'"));
        }
    }
    if let Some(name) = pipeline.target.get("name").and_then(yamlx::as_string) {
        if !target_names.is_empty() && !target_names.contains(&name) {
            errors.push(format!("target.name '{name}' is not present in targets"));
        }
    }

    let mut seen: HashSet<&String> = HashSet::new();
    for step in &pipeline.steps {
        if !seen.insert(&step.id) {
            errors.push(format!("duplicate step id: {}", step.id));
        }
        if !known_step_types.contains(&step.step_type) {
            errors.push(format!("unknown step type '{}' in step '{}'", step.step_type, step.id));
        }
        if !VALID_RISKS.contains(&step.risk.as_str()) {
            errors.push(format!("step '{}' has invalid risk '{}'", step.id, step.risk));
        }
        if !step.repo.is_empty() && !repo_ids.contains(&step.repo) {
            errors.push(format!("step '{}' references unknown repo '{}'", step.id, step.repo));
        }

        for dep in &step.needs {
            if !step_ids.contains(dep) {
                errors.push(format!("step '{}' depends on missing step '{}'", step.id, dep));
            }
            if dep == &step.id {
                errors.push(format!("step '{}' depends on itself", step.id));
            }
        }
        if !VALID_RUN_IF.contains(&step.run_if.as_str()) {
            errors.push(format!("step '{}' has invalid run_if '{}'; expected one of {:?}", step.id, step.run_if, VALID_RUN_IF));
        }
        let exclusive: HashSet<&String> = step.exclusive_resources.iter().collect();
        for resource in &step.shared_resources {
            if exclusive.contains(resource) {
                errors.push(format!("step '{}' declares resource '{}' as both exclusive and shared", step.id, resource));
            }
        }
        for name in step.exclusive_resources.iter().chain(step.shared_resources.iter()).chain(step.requires.iter()).chain(step.provides.iter()) {
            if name.trim().is_empty() {
                errors.push(format!("step '{}' contains an empty resource/capability name", step.id));
            }
        }

        match step.step_type.as_str() {
            "command" => validate_command_step(step, &mut errors),
            "plugin" => validate_plugin_step(step, &mut errors),
            "copy_or_sync" => validate_copy_or_sync_step(step, &mut errors),
            _ => {}
        }
    }

    validate_error_policy(pipeline, known_step_types, &step_ids, &mut errors);
    errors
}

fn validate_command_step(step: &PipelineStep, errors: &mut Vec<String>) {
    let has_argv = validate_argv_like(step, "argv", errors);
    let has_command = !step.command.is_empty()
        || step.with.get("command").and_then(yamlx::as_string).is_some();
    if !has_argv && !has_command {
        errors.push(format!(
            "command step '{}' is missing a command: set `with.argv: [...]` or `with.command: \"...\"`",
            step.id
        ));
    }
    validate_u64_field(step, "timeout", 1, errors);
    validate_u64_field(step, "inactivity_timeout", 1, errors);
    validate_u64_field(step, "retries", 0, errors);
    validate_u64_field(step, "retry_delay", 0, errors);
    if let Some(env) = step.with.get("env") {
        if !env.is_mapping() {
            errors.push(format!("command step '{}' has invalid with.env; expected mapping", step.id));
        }
    }
}

fn validate_plugin_step(step: &PipelineStep, errors: &mut Vec<String>) {
    if !validate_argv_like(step, "argv", errors) {
        errors.push(format!("plugin step '{}' requires `with.argv: [...]`", step.id));
    }
    validate_u64_field(step, "timeout", 1, errors);
    validate_u64_field(step, "inactivity_timeout", 1, errors);
    validate_u64_field(step, "retries", 0, errors);
    validate_u64_field(step, "retry_delay", 0, errors);
}

fn validate_copy_or_sync_step(step: &PipelineStep, errors: &mut Vec<String>) {
    if step.with.get("destination").and_then(yamlx::as_string).filter(|s| !s.trim().is_empty()).is_none() {
        errors.push(format!("copy_or_sync step '{}' requires `with.destination`", step.id));
    }
    if let Some(mode) = step.with.get("mode").and_then(yamlx::as_string) {
        if !["copy", "sync"].contains(&mode.as_str()) {
            errors.push(format!("copy_or_sync step '{}' has invalid mode '{}'; use copy or sync", step.id, mode));
        }
    }
    validate_u64_field(step, "max_files", 1, errors);
}

/// Returns true only when argv exists and is valid.
fn validate_argv_like(step: &PipelineStep, key: &str, errors: &mut Vec<String>) -> bool {
    let Some(argv) = step.with.get(key) else {
        return false;
    };
    let Some(seq) = argv.as_sequence() else {
        errors.push(format!("step '{}' has invalid with.{key}; expected non-empty list of strings", step.id));
        return false;
    };
    if seq.is_empty() {
        errors.push(format!("step '{}' has invalid with.{key}; list cannot be empty", step.id));
        return false;
    }
    let mut ok = true;
    for (i, item) in seq.iter().enumerate() {
        let valid = yamlx::as_string(item).map(|s| !s.trim().is_empty()).unwrap_or(false);
        if !valid {
            errors.push(format!("step '{}' has invalid with.{key}[{i}]; expected non-empty scalar", step.id));
            ok = false;
        }
    }
    ok
}

fn validate_u64_field(step: &PipelineStep, field: &str, min: u64, errors: &mut Vec<String>) {
    let Some(value) = step.with.get(field) else {
        return;
    };
    let parsed = match value {
        serde_yaml::Value::Number(n) => n.as_u64(),
        serde_yaml::Value::String(s) => s.trim().parse::<u64>().ok(),
        _ => None,
    };
    match parsed {
        Some(n) if n >= min => {}
        Some(_) => errors.push(format!("step '{}' has invalid with.{field}; must be >= {min}", step.id)),
        None => errors.push(format!("step '{}' has invalid with.{field}; expected integer seconds/count", step.id)),
    }
}

fn validate_error_policy(
    pipeline: &PipelineDefinition,
    known_step_types: &HashSet<String>,
    step_ids: &HashSet<&String>,
    errors: &mut Vec<String>,
) {
    let policy = &pipeline.on_error;
    if !VALID_ERROR_POLICIES.contains(&policy.default.as_str()) {
        errors.push(format!("on_error.default must be one of {VALID_ERROR_POLICIES:?}"));
    }
    for (name, map) in [
        ("steps", &policy.steps),
        ("types", &policy.types),
        ("statuses", &policy.statuses),
    ] {
        for (key, value) in map {
            if !VALID_ERROR_POLICIES.contains(&value.as_str()) {
                errors.push(format!("on_error.{name}.{key} has invalid policy '{value}'"));
            }
        }
    }
    for key in policy.steps.keys() {
        if !step_ids.iter().any(|id| id.as_str() == key.as_str()) {
            errors.push(format!("on_error.steps.{key} references an unknown step id"));
        }
    }
    for key in policy.types.keys() {
        if !known_step_types.contains(key) {
            if step_ids.iter().any(|id| id.as_str() == key.as_str()) {
                errors.push(format!(
                    "on_error.types.{key} looks like a step id, not a step type; use on_error.steps.{key} instead"
                ));
            } else {
                errors.push(format!("on_error.types.{key} references an unknown step type"));
            }
        }
    }
    for key in policy.statuses.keys() {
        if !VALID_STATUSES.contains(&key.as_str()) {
            errors.push(format!("on_error.statuses.{key} references an unknown status"));
        }
    }
}
