//! Label extraction and YAML route matching.
//!
//! V1 extracted only `#labels`. V1.1 accepts both `#labels` and bare tokens
//! (`ci`, `release`, `scraper`) so commands like `pipecraft route ci` behave as
//! users expect. If a string contains one or more hash labels, only hash labels
//! are returned; this preserves legacy behaviour for free-form commit messages.

use std::path::Path;

use serde_yaml::Value;

use crate::error::{ConfigError, ConfigResult};
use crate::loader::load_workspace;
use crate::model::*;
use crate::yamlx::{self, get};

fn is_word_char(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

fn is_label_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-')
}

fn push_label(out: &mut Vec<String>, seen: &mut std::collections::HashSet<String>, raw: &str) {
    let label = raw.trim().trim_start_matches('#').to_ascii_lowercase();
    if label.is_empty() || !label.chars().all(is_label_char) {
        return;
    }
    if seen.insert(label.clone()) {
        out.push(label);
    }
}

/// Extract unique labels from a free-form string.
///
/// Rules:
/// - `#ci #build` -> `ci`, `build`.
/// - `ci build` -> `ci`, `build`.
/// - `color#fff` yields no hash label because `#` is preceded by a word char.
/// - If any hash labels are present, bare words are ignored to avoid turning a
///   whole commit subject into labels.
/// - Labels are normalised to lowercase.
pub fn extract_labels(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let mut seen = std::collections::HashSet::new();
    let mut hash_labels = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '#' {
            let preceded_by_word = i > 0 && is_word_char(chars[i - 1]);
            if !preceded_by_word {
                let start = i + 1;
                let mut j = start;
                while j < chars.len() && is_label_char(chars[j]) {
                    j += 1;
                }
                if j > start {
                    let label: String = chars[start..j].iter().collect();
                    push_label(&mut hash_labels, &mut seen, &label);
                    i = j;
                    continue;
                }
            }
        }
        i += 1;
    }
    if !hash_labels.is_empty() {
        return hash_labels;
    }

    let mut bare = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for token in text.split_whitespace() {
        let cleaned = token.trim_matches(|c: char| !(is_label_char(c) || c == '#'));
        if cleaned.is_empty() || cleaned.contains('#') {
            continue;
        }
        push_label(&mut bare, &mut seen, cleaned);
    }
    bare
}

fn read_routes_yaml(path: &Path) -> ConfigResult<Value> {
    let text = std::fs::read_to_string(path).map_err(|e| {
        ConfigError::new(
            format!("Could not read routes file: {} ({e})", path.display()),
            "ROUTES_READ_ERROR",
        )
        .with_detail("path", path.display().to_string())
    })?;
    let value: Value = serde_yaml::from_str(&text).map_err(|e| {
        ConfigError::new(
            format!("Invalid YAML syntax in routes file {}: {e}", path.display()),
            "ROUTES_PARSE_ERROR",
        )
        .with_hint("Check indentation under `routes:` and list markers under `when.labels`.")
        .with_detail("path", path.display().to_string())
    })?;
    match value {
        Value::Null => Ok(Value::Mapping(Default::default())),
        Value::Mapping(_) => Ok(value),
        _ => Err(ConfigError::new(
            format!("Routes YAML root must be a mapping: {}", path.display()),
            "ROUTES_ROOT_ERROR",
        )
        .with_hint("The top level of routes.yaml must contain `routes:`.")),
    }
}

/// Load label routes from `.pipelines/routes.yaml`.
pub fn load_routes(root: &Path, workspace: Option<&WorkspaceConfig>) -> ConfigResult<Vec<RouteDefinition>> {
    let owned;
    let ws = match workspace {
        Some(w) => w,
        None => {
            owned = load_workspace(root)?;
            &owned
        }
    };
    let path = root.join(&ws.route_file);
    if !path.exists() {
        return Ok(Vec::new());
    }
    let raw = read_routes_yaml(&path)?;
    let routes_value = get(&raw, "routes").cloned().unwrap_or(Value::Null);
    let items = yamlx::as_list(&routes_value, "routes")?;

    let mut routes = Vec::with_capacity(items.len());
    for (i, item) in items.iter().enumerate() {
        let index = i + 1;
        if !item.is_mapping() {
            return Err(ConfigError::new(
                format!("Route #{index} in {} must be a mapping", path.display()),
                "ROUTE_TYPE_ERROR",
            )
            .with_hint("Each route must be a YAML mapping with `id`, `when`, and `pipeline`."));
        }
        let when = get(item, "when").cloned().unwrap_or(Value::Null);
        let labels = get(&when, "labels").cloned().unwrap_or(Value::Null);

        // `when.labels.{include,any|any_of,exclude}` with top-level fallbacks.
        let include = pick_list(&labels, "include").or_else(|| pick_list(item, "include")).unwrap_or_default();
        let any_of = pick_list(&labels, "any")
            .or_else(|| pick_list(&labels, "any_of"))
            .or_else(|| pick_list(item, "any_of"))
            .unwrap_or_default();
        let exclude = pick_list(&labels, "exclude").or_else(|| pick_list(item, "exclude")).unwrap_or_default();

        let pipeline = get(item, "pipeline").and_then(yamlx::as_string).unwrap_or_default();
        if pipeline.trim().is_empty() {
            return Err(ConfigError::new(
                format!("Route #{index} in {} is missing `pipeline`", path.display()),
                "ROUTE_PIPELINE_MISSING",
            )
            .with_hint("Set `pipeline: <pipeline-name>` on every route."));
        }

        routes.push(RouteDefinition {
            id: get(item, "id").and_then(yamlx::as_string).unwrap_or_else(|| format!("route_{index}")),
            pipeline,
            include: strip_hashes(include),
            any_of: strip_hashes(any_of),
            exclude: strip_hashes(exclude),
            description: get(item, "description").and_then(yamlx::as_string).unwrap_or_default(),
            set_context: get(item, "set_context")
                .map(|v| yamlx::as_mapping(v, "set_context"))
                .transpose()?
                .unwrap_or_default(),
        });
    }
    Ok(routes)
}

fn pick_list(value: &Value, key: &str) -> Option<Vec<String>> {
    get(value, key).and_then(|v| yamlx::as_string_list(v, key).ok())
}

fn strip_hashes(items: Vec<String>) -> Vec<String> {
    items.into_iter().map(|s| s.trim_start_matches('#').to_ascii_lowercase()).collect()
}

/// Every route satisfied by a label set, evaluated in YAML order.
pub fn match_routes(labels: &[String], routes: &[RouteDefinition]) -> Vec<RouteMatch> {
    let normalized: Vec<String> = labels.iter().map(|l| l.to_ascii_lowercase()).collect();
    let label_set: std::collections::HashSet<&String> = normalized.iter().collect();
    let mut matches = Vec::new();
    for route in routes {
        if route.pipeline.is_empty() {
            continue;
        }
        if !route.include.is_empty() && !route.include.iter().all(|l| label_set.contains(l)) {
            continue;
        }
        if !route.any_of.is_empty() && !route.any_of.iter().any(|l| label_set.contains(l)) {
            continue;
        }
        if !route.exclude.is_empty() && route.exclude.iter().any(|l| label_set.contains(l)) {
            continue;
        }
        matches.push(RouteMatch {
            route: route.clone(),
            labels: normalized.clone(),
            pipeline: route.pipeline.clone(),
            context: route.set_context.clone(),
        });
    }
    matches
}

/// Pick a route, with warnings about ambiguity or no-match.
pub fn select_route(
    labels: &[String],
    routes: &[RouteDefinition],
    default_pipeline: &str,
) -> (Option<RouteMatch>, Vec<String>) {
    let matches = match_routes(labels, routes);
    let mut warnings = Vec::new();

    if matches.is_empty() {
        if !default_pipeline.is_empty() {
            warnings.push(format!(
                "No route matched labels {labels:?}; using default pipeline '{default_pipeline}'."
            ));
            let fallback = RouteMatch {
                route: RouteDefinition {
                    id: "__default__".into(),
                    pipeline: default_pipeline.into(),
                    include: vec![],
                    any_of: vec![],
                    exclude: vec![],
                    description: String::new(),
                    set_context: Default::default(),
                },
                labels: labels.to_vec(),
                pipeline: default_pipeline.into(),
                context: Default::default(),
            };
            return (Some(fallback), warnings);
        }
        warnings.push(format!("No route matched labels {labels:?}."));
        return (None, warnings);
    }

    if matches.len() > 1 {
        let summary: Vec<String> =
            matches.iter().map(|m| format!("{}->{}", m.route.id, m.pipeline)).collect();
        warnings.push(format!(
            "Multiple routes matched; using first match by routes.yaml order: {}",
            summary.join(", ")
        ));
    }
    (Some(matches.into_iter().next().unwrap()), warnings)
}
