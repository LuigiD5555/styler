use crate::catalog::{Catalog, ComponentDefinition};
use crate::protocol::{DiagnosticIssue, DiagnosticReport};
use std::collections::{BTreeMap, BTreeSet};

const KNOWN_PROVIDER_TYPES: &[&str] = &[
    "apt", "deb", "pacman", "rpm", "dnf", "zypper", "flatpak", "snap", "appimage",
    "archive", "binary", "local", "github_release", "gitlab_release", "nix", "guix", "brew",
];

pub fn diagnose(catalog: &Catalog) -> DiagnosticReport {
    let mut issues = Vec::new();
    for duplicate in &catalog.duplicate_ids {
        issues.push(issue(
            "error",
            "DUPLICATE_COMPONENT_ID",
            duplicate,
            "",
            "id",
            format!("el ID '{duplicate}' aparece más de una vez"),
            "Conserva una sola definición o usa un namespace distinto.",
        ));
    }

    for warning in &catalog.warnings {
        issues.push(issue(
            "warning",
            "CATALOG_LOAD_WARNING",
            "",
            catalog.root.to_str().unwrap_or(""),
            "index",
            warning.clone(),
            "Agrega o corrige index.toml para una carga determinista.",
        ));
    }
    for orphan in &catalog.orphan_files {
        issues.push(issue(
            "warning",
            "ORPHAN_COMPONENT_FILE",
            "",
            orphan,
            "index",
            "el archivo TOML no está registrado en index.toml".to_string(),
            "Regístralo en [components] o elimínalo del catálogo.",
        ));
    }

    for component in catalog.components.values() {
        validate_component(component, catalog, &mut issues);
    }
    for cycle in detect_component_cycles(catalog) {
        issues.push(issue(
            "error",
            "DEPENDENCY_CYCLE",
            cycle.first().map(String::as_str).unwrap_or(""),
            "",
            "requires",
            format!("ciclo de dependencias: {}", cycle.join(" -> ")),
            "Rompe el ciclo o convierte una dependencia estricta en opcional.",
        ));
    }
    issues.sort_by(|left, right| {
        (&left.severity, &left.code, &left.component_id, &left.path)
            .cmp(&(&right.severity, &right.code, &right.component_id, &right.path))
    });
    let errors = issues.iter().filter(|item| item.severity == "error").count();
    let warnings = issues.iter().filter(|item| item.severity == "warning").count();
    DiagnosticReport {
        catalog_root: catalog.root.to_string_lossy().to_string(),
        components: catalog.components.len(),
        errors,
        warnings,
        issues,
    }
}

fn validate_component(component: &ComponentDefinition, catalog: &Catalog, issues: &mut Vec<DiagnosticIssue>) {
    if component.schema_version != 1 {
        issues.push(issue(
            "error", "UNSUPPORTED_SCHEMA_VERSION", &component.id, &component.source_path, "schema_version",
            format!("schema_version {} no está soportado", component.schema_version),
            "Usa schema_version = 1 durante esta iteración.",
        ));
    }
    if !component.id.contains('.') {
        issues.push(issue(
            "warning", "ID_WITHOUT_NAMESPACE", &component.id, &component.source_path, "id",
            "el ID no usa namespace".to_string(), "Usa un ID como app.gimp o desktop.kde.plasma.",
        ));
    }
    if component.requires.iter().any(|value| value == &component.id) {
        issues.push(issue(
            "error", "SELF_DEPENDENCY", &component.id, &component.source_path, "requires",
            "el componente se requiere a sí mismo".to_string(), "Elimina la dependencia circular.",
        ));
    }
    for requirement in &component.requires {
        if catalog.providers_for(requirement).is_empty() {
            issues.push(issue(
                if component.required() { "error" } else { "warning" },
                "MISSING_CAPABILITY_PROVIDER", &component.id, &component.source_path, "requires",
                format!("ningún componente proporciona '{requirement}'"),
                "Agrega un proveedor de la capacidad o corrige el nombre.",
            ));
        }
    }
    for (provider_id, provider) in &component.providers {
        if !KNOWN_PROVIDER_TYPES.contains(&provider.provider_type.as_str()) {
            issues.push(issue(
                "error", "UNKNOWN_PROVIDER_TYPE", &component.id, &component.source_path, "providers",
                format!("el proveedor '{provider_id}' usa el tipo desconocido '{}'", provider.provider_type),
                "Agrega el adaptador al motor o usa un tipo reconocido.",
            ));
        }
        if provider.families.is_empty() {
            issues.push(issue(
                "warning", "PROVIDER_WITHOUT_FAMILIES", &component.id, &component.source_path, "providers.families",
                format!("el proveedor '{provider_id}' no declara familias"), "Usa ['*'] cuando sea universal.",
            ));
        }
        if matches!(provider.provider_type.as_str(), "apt" | "deb" | "pacman" | "rpm" | "dnf" | "zypper" | "snap")
            && provider.packages.is_empty()
        {
            issues.push(issue(
                "error", "EMPTY_PACKAGE_LIST", &component.id, &component.source_path, "providers.packages",
                format!("el proveedor '{provider_id}' no declara paquetes"), "Agrega al menos un paquete.",
            ));
        }
        if provider.provider_type == "flatpak" && provider.application_id.is_empty() {
            issues.push(issue(
                "error", "MISSING_APPLICATION_ID", &component.id, &component.source_path, "providers.application_id",
                format!("el proveedor Flatpak '{provider_id}' no declara application_id"),
                "Agrega el ID completo de Flatpak.",
            ));
        }
    }
    if component.required() && !component.providers.is_empty() && component.verification.checks.is_empty() {
        issues.push(issue(
            "error", "MISSING_VERIFICATION", &component.id, &component.source_path, "verification",
            "un componente requerido instalable no declara comprobaciones".to_string(),
            "Agrega checks verificables.",
        ));
    }
    if matches!(component.rollback.level.as_str(), "full" | "best_effort") && component.rollback.strategy.is_empty() {
        issues.push(issue(
            "error", "MISSING_ROLLBACK_STRATEGY", &component.id, &component.source_path, "rollback.strategy",
            "el rollback declarado no tiene estrategia".to_string(), "Declara una estrategia concreta.",
        ));
    }
}

fn detect_component_cycles(catalog: &Catalog) -> Vec<Vec<String>> {
    let mut graph: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for component in catalog.components.values() {
        let targets = graph.entry(component.id.clone()).or_default();
        for requirement in &component.requires {
            for provider in catalog.providers_for(requirement) {
                if provider.id != component.id {
                    targets.insert(provider.id.clone());
                }
            }
        }
    }
    let mut visited = BTreeSet::new();
    let mut on_stack = BTreeSet::new();
    let mut stack = Vec::new();
    let mut cycles = Vec::new();
    for node in graph.keys() {
        visit(node, &graph, &mut visited, &mut on_stack, &mut stack, &mut cycles);
    }
    cycles
}

fn visit(
    node: &str,
    graph: &BTreeMap<String, BTreeSet<String>>,
    visited: &mut BTreeSet<String>,
    on_stack: &mut BTreeSet<String>,
    stack: &mut Vec<String>,
    cycles: &mut Vec<Vec<String>>,
) {
    if visited.contains(node) {
        return;
    }
    visited.insert(node.to_string());
    on_stack.insert(node.to_string());
    stack.push(node.to_string());
    if let Some(neighbors) = graph.get(node) {
        for neighbor in neighbors {
            if !visited.contains(neighbor) {
                visit(neighbor, graph, visited, on_stack, stack, cycles);
            } else if on_stack.contains(neighbor) {
                if let Some(start) = stack.iter().position(|item| item == neighbor) {
                    let mut cycle = stack[start..].to_vec();
                    cycle.push(neighbor.clone());
                    if !cycles.contains(&cycle) {
                        cycles.push(cycle);
                    }
                }
            }
        }
    }
    stack.pop();
    on_stack.remove(node);
}

fn issue(
    severity: &str,
    code: &str,
    component_id: &str,
    path: &str,
    field: &str,
    message: String,
    suggestion: &str,
) -> DiagnosticIssue {
    DiagnosticIssue {
        severity: severity.to_string(),
        code: code.to_string(),
        component_id: component_id.to_string(),
        path: path.to_string(),
        field: field.to_string(),
        message,
        suggestion: suggestion.to_string(),
    }
}
