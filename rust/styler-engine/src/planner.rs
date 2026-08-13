use crate::catalog::{Catalog, ComponentDefinition, ProviderDefinition};
use crate::host::detect_host;
use crate::protocol::{
    PlanIssue, PlanRequest, PlanResult, PlanStep, ResolutionDecision, TargetRequestOutput,
};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

pub fn build_plan(catalog: &Catalog, request: PlanRequest) -> PlanResult {
    let host = detect_host();
    let family = if request.target.family.is_empty() { host.family } else { request.target.family.clone() };
    let architecture = if request.target.architecture.is_empty() {
        host.architecture
    } else {
        request.target.architecture.clone()
    };
    let home = if request.target.home.is_empty() { host.home } else { request.target.home.clone() };
    let allowed_types: BTreeSet<String> = request.allowed_provider_types.iter().cloned().collect();
    let mut selected = BTreeSet::new();
    let mut selected_providers = BTreeMap::new();
    let mut decisions = Vec::new();
    let mut missing = BTreeSet::new();
    let mut issues = Vec::new();
    let mut requirement_provider: BTreeMap<(String, String), String> = BTreeMap::new();
    let mut queue: VecDeque<String> = request.desired_components.iter().cloned().collect();

    while let Some(component_id) = queue.pop_front() {
        if selected.contains(&component_id) {
            continue;
        }
        let Some(component) = catalog.components.get(&component_id) else {
            issues.push(PlanIssue {
                severity: "error".to_string(),
                code: "UNKNOWN_COMPONENT".to_string(),
                component_id: component_id.clone(),
                message: format!("el componente solicitado '{component_id}' no existe"),
            });
            continue;
        };
        selected.insert(component_id.clone());
        match choose_provider(
            component,
            &family,
            request.provider_preferences.get(&component_id),
            &allowed_types,
        ) {
            Some((provider_id, _, reason)) => {
                selected_providers.insert(component_id.clone(), provider_id.clone());
                decisions.push(ResolutionDecision {
                    component_id: component_id.clone(),
                    requirement: String::new(),
                    candidates: component.providers.keys().cloned().collect(),
                    chosen_component: component_id.clone(),
                    chosen_provider: provider_id,
                    reason,
                });
            }
            None if !component.providers.is_empty() => issues.push(PlanIssue {
                severity: if component.required() { "error" } else { "warning" }.to_string(),
                code: "NO_COMPATIBLE_PROVIDER".to_string(),
                component_id: component_id.clone(),
                message: format!("no hay proveedor compatible para la familia '{family}'"),
            }),
            None => {}
        }

        for requirement in &component.requires {
            let mut candidates: Vec<&ComponentDefinition> = catalog.providers_for(requirement);
            candidates.sort_by(|left, right| left.id.cmp(&right.id));
            let candidate_ids: Vec<String> = candidates.iter().map(|item| item.id.clone()).collect();
            let chosen = candidates.into_iter().find_map(|candidate| {
                choose_provider(
                    candidate,
                    &family,
                    request.provider_preferences.get(&candidate.id),
                    &allowed_types,
                )
                .map(|(provider_id, _, _)| (candidate.id.clone(), provider_id))
                .or_else(|| {
                    if candidate.providers.is_empty() {
                        Some((candidate.id.clone(), String::new()))
                    } else {
                        None
                    }
                })
            });
            if let Some((chosen_component, chosen_provider)) = chosen {
                requirement_provider.insert(
                    (component_id.clone(), requirement.clone()),
                    chosen_component.clone(),
                );
                queue.push_back(chosen_component.clone());
                decisions.push(ResolutionDecision {
                    component_id: component_id.clone(),
                    requirement: requirement.clone(),
                    candidates: candidate_ids,
                    chosen_component,
                    chosen_provider,
                    reason: format!("proveedor compatible con la familia '{family}'"),
                });
            } else {
                missing.insert(requirement.clone());
                issues.push(PlanIssue {
                    severity: if component.required() { "error" } else { "warning" }.to_string(),
                    code: "MISSING_CAPABILITY".to_string(),
                    component_id: component_id.clone(),
                    message: format!("no se pudo satisfacer '{requirement}'"),
                });
                decisions.push(ResolutionDecision {
                    component_id: component_id.clone(),
                    requirement: requirement.clone(),
                    candidates: candidate_ids,
                    chosen_component: String::new(),
                    chosen_provider: String::new(),
                    reason: "no existe un componente con proveedor compatible".to_string(),
                });
            }
        }
    }

    let mut steps = Vec::new();
    let mut satisfied = BTreeSet::new();
    for component_id in &selected {
        if let Some(component) = catalog.components.get(component_id) {
            satisfied.extend(component.provides.iter().cloned());
            let provider_id = selected_providers.get(component_id).cloned().unwrap_or_default();
            let provider = component.providers.get(&provider_id);
            let mut needs = Vec::new();
            for requirement in &component.requires {
                if let Some(provider_component) = requirement_provider.get(&(component_id.clone(), requirement.clone())) {
                    needs.push(dependency_step_id(requirement, provider_component));
                }
            }
            needs.sort();
            needs.dedup();
            let target_root = target_root_for(
                component,
                catalog,
                &selected_providers,
                &requirement_provider,
                &home,
            );
            let backup_needed = needs_backup(component);
            let install_id = install_step_id(component_id);
            let install_needs = if backup_needed {
                let backup_id = backup_step_id(component_id);
                steps.push(PlanStep {
                    id: backup_id.clone(),
                    step_type: "backup_config".to_string(),
                    description: format!("Respaldar configuración antes de aplicar {}", component.name),
                    needs: needs.clone(),
                    requires: component.requires.clone(),
                    provides: vec![format!("backup.{}.ready", component.id)],
                    exclusive_resources: component.resources.exclusive.clone(),
                    shared_resources: component.resources.shared.clone(),
                    criticality: if component.required() { "critical" } else { "normal" }.to_string(),
                    stage: "backup".to_string(),
                    required: component.required(),
                    provider: provider_id.clone(),
                    config: serde_json::json!({ "target": target_root.clone(), "home": home.clone() }),
                    rollback: serde_json::to_value(&component.rollback).unwrap_or(serde_json::Value::Null),
                });
                vec![backup_id]
            } else {
                needs
            };
            let mut config = provider_config(provider_id.as_str(), provider, &home);
            if !target_root.is_empty() {
                if let Some(object) = config.as_object_mut() {
                    object.insert("target".to_string(), serde_json::Value::String(target_root.clone()));
                }
            }
            steps.push(PlanStep {
                id: install_id.clone(),
                step_type: step_type(component, provider),
                description: format!("Instalar/aplicar {}", component.name),
                needs: install_needs,
                requires: if backup_needed { Vec::new() } else { component.requires.clone() },
                provides: component
                    .provides
                    .iter()
                    .filter(|item| !item.ends_with(".verified"))
                    .cloned()
                    .collect(),
                exclusive_resources: component.resources.exclusive.clone(),
                shared_resources: component.resources.shared.clone(),
                criticality: if component.required() { "critical" } else { "normal" }.to_string(),
                stage: "install".to_string(),
                required: component.required(),
                provider: provider_id.clone(),
                config,
                rollback: serde_json::to_value(&component.rollback).unwrap_or(serde_json::Value::Null),
            });
            steps.push(PlanStep {
                id: verify_step_id(component_id),
                step_type: "verify".to_string(),
                description: format!("Verificar {}", component.name),
                needs: vec![install_id],
                requires: Vec::new(),
                provides: component
                    .provides
                    .iter()
                    .filter(|item| item.ends_with(".verified"))
                    .cloned()
                    .collect(),
                exclusive_resources: Vec::new(),
                shared_resources: Vec::new(),
                criticality: if component.required() { "critical" } else { "normal" }.to_string(),
                stage: "verify".to_string(),
                required: component.required(),
                provider: provider_id,
                config: serde_json::json!({
                    "checks": component.verification.checks.clone(),
                    "home": home.clone(),
                    "target": target_root.clone(),
                }),
                rollback: serde_json::Value::Null,
            });
        }
    }

    let (order, cycle) = topological_order(&steps);
    if !cycle.is_empty() {
        issues.push(PlanIssue {
            severity: "error".to_string(),
            code: "STEP_CYCLE".to_string(),
            component_id: cycle.first().cloned().unwrap_or_default(),
            message: format!("ciclo entre pasos: {}", cycle.join(" -> ")),
        });
    }
    let executable = !issues.iter().any(|item| item.severity == "error");
    PlanResult {
        name: request.name,
        target: TargetRequestOutput { family, architecture, home },
        selected_components: selected.into_iter().collect(),
        selected_providers,
        satisfied_capabilities: satisfied.into_iter().collect(),
        missing_capabilities: missing.into_iter().collect(),
        decisions,
        order,
        steps,
        issues,
        executable,
    }
}

fn choose_provider<'a>(
    component: &'a ComponentDefinition,
    family: &str,
    preference: Option<&String>,
    allowed_types: &BTreeSet<String>,
) -> Option<(String, &'a ProviderDefinition, String)> {
    let mut candidates: Vec<(&String, &ProviderDefinition)> = component
        .providers
        .iter()
        .filter(|(_, provider)| {
            (provider.families.iter().any(|value| value == "*")
                || provider.families.iter().any(|value| value == family))
                && (allowed_types.is_empty() || allowed_types.contains(&provider.provider_type))
        })
        .collect();
    if let Some(preferred_id) = preference {
        if let Some((id, provider)) = candidates.iter().find(|(id, _)| *id == preferred_id) {
            return Some(((*id).clone(), *provider, "preferencia explícita del usuario".to_string()));
        }
    }
    candidates.sort_by(|(left_id, left), (right_id, right)| {
        right.priority.cmp(&left.priority).then_with(|| left_id.cmp(right_id))
    });
    candidates.first().map(|(id, provider)| {
        ((*id).clone(), *provider, "mayor prioridad compatible".to_string())
    })
}

fn step_type(component: &ComponentDefinition, provider: Option<&ProviderDefinition>) -> String {
    if component.kind == "configuration" {
        "apply_config".to_string()
    } else if component.kind == "application_overlay" {
        "overlay_install".to_string()
    } else {
        match provider.map(|item| item.provider_type.as_str()).unwrap_or("") {
            "apt" | "deb" => "install_apt",
            "pacman" => "install_pacman",
            "rpm" | "dnf" => "install_rpm",
            "zypper" => "install_zypper",
            "flatpak" => "install_flatpak",
            "snap" => "install_snap",
            "appimage" => "install_appimage",
            "archive" => "install_archive",
            "binary" | "local" => "install_file",
            _ => "install_component",
        }
        .to_string()
    }
}

fn provider_config(provider_id: &str, provider: Option<&ProviderDefinition>, home: &str) -> serde_json::Value {
    let Some(provider) = provider else {
        return serde_json::json!({ "provider_id": provider_id, "home": home });
    };
    serde_json::json!({
        "provider_id": provider_id,
        "provider_type": provider.provider_type.clone(),
        "families": provider.families.clone(),
        "packages": provider.packages.clone(),
        "application_id": provider.application_id.clone(),
        "source": provider.source.clone(),
        "checksum_sha256": provider.checksum_sha256.clone(),
        "artifact_kind": provider.artifact_kind.clone(),
        "destination": provider.destination.replace("${HOME}", home),
        "file_name": provider.file_name.clone(),
        "strip_components": provider.strip_components,
        "max_size_bytes": provider.max_size_bytes,
        "desktop_entry": provider.desktop_entry,
        "executable_name": provider.executable_name.clone(),
        "config_root": provider.config_root.replace("${HOME}", home),
        "home": home,
    })
}


fn dependency_step_id(capability: &str, component_id: &str) -> String {
    if capability.ends_with(".installed") && !capability.ends_with(".verified") {
        install_step_id(component_id)
    } else {
        verify_step_id(component_id)
    }
}

fn backup_step_id(component_id: &str) -> String {
    format!("backup:{component_id}")
}

fn needs_backup(component: &ComponentDefinition) -> bool {
    matches!(component.kind.as_str(), "application_overlay" | "configuration")
        && matches!(component.rollback.level.as_str(), "full" | "best_effort")
}

fn target_root_for(
    component: &ComponentDefinition,
    catalog: &Catalog,
    selected_providers: &BTreeMap<String, String>,
    requirement_provider: &BTreeMap<(String, String), String>,
    home: &str,
) -> String {
    for resource in &component.resources.exclusive {
        if let Some(path) = component.resources.paths.get(resource) {
            if !path.is_empty() {
                return path.replace("${HOME}", home);
            }
        }
    }
    for requirement in &component.requires {
        let key = (component.id.clone(), requirement.clone());
        let Some(provider_component_id) = requirement_provider.get(&key) else {
            continue;
        };
        let Some(provider_component) = catalog.components.get(provider_component_id) else {
            continue;
        };
        let Some(provider_id) = selected_providers.get(provider_component_id) else {
            continue;
        };
        let Some(provider) = provider_component.providers.get(provider_id) else {
            continue;
        };
        if !provider.config_root.is_empty() {
            return provider.config_root.replace("${HOME}", home);
        }
    }
    String::new()
}

fn install_step_id(component_id: &str) -> String {
    format!("install:{component_id}")
}

fn verify_step_id(component_id: &str) -> String {
    format!("verify:{component_id}")
}

fn topological_order(steps: &[PlanStep]) -> (Vec<String>, Vec<String>) {
    let by_id: BTreeMap<String, &PlanStep> = steps.iter().map(|step| (step.id.clone(), step)).collect();
    let mut indegree: BTreeMap<String, usize> = by_id.keys().map(|id| (id.clone(), 0)).collect();
    let mut dependents: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for step in steps {
        for dependency in &step.needs {
            if by_id.contains_key(dependency) {
                *indegree.entry(step.id.clone()).or_default() += 1;
                dependents.entry(dependency.clone()).or_default().push(step.id.clone());
            }
        }
    }
    let mut ready: BTreeSet<String> = indegree
        .iter()
        .filter(|(_, value)| **value == 0)
        .map(|(id, _)| id.clone())
        .collect();
    let mut order = Vec::new();
    while let Some(id) = ready.iter().next().cloned() {
        ready.remove(&id);
        order.push(id.clone());
        if let Some(children) = dependents.get(&id) {
            for child in children {
                if let Some(value) = indegree.get_mut(child) {
                    *value -= 1;
                    if *value == 0 {
                        ready.insert(child.clone());
                    }
                }
            }
        }
    }
    if order.len() == steps.len() {
        return (order, Vec::new());
    }
    let cycle = indegree
        .into_iter()
        .filter(|(_, value)| *value > 0)
        .map(|(id, _)| id)
        .collect();
    (order, cycle)
}

#[cfg(test)]
mod tests {
    use super::topological_order;
    use crate::protocol::PlanStep;

    fn step(id: &str, needs: &[&str]) -> PlanStep {
        PlanStep {
            id: id.to_string(),
            step_type: "test".to_string(),
            description: String::new(),
            needs: needs.iter().map(|value| value.to_string()).collect(),
            requires: Vec::new(),
            provides: Vec::new(),
            exclusive_resources: Vec::new(),
            shared_resources: Vec::new(),
            criticality: "normal".to_string(),
            stage: "test".to_string(),
            required: true,
            provider: String::new(),
            config: serde_json::Value::Null,
            rollback: serde_json::Value::Null,
        }
    }

    #[test]
    fn orders_dependencies_before_dependents() {
        let (order, cycle) = topological_order(&[step("b", &["a"]), step("a", &[])]);
        assert!(cycle.is_empty());
        assert_eq!(order, vec!["a", "b"]);
    }
}
