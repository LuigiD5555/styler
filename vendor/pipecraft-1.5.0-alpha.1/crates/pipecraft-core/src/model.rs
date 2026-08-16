//! Stable internal domain model.
//!
//! These types are deliberately decoupled from the YAML surface: the loader
//! (see `loader.rs`) parses raw YAML and produces these, so we can give precise,
//! coded errors instead of opaque serde failures. The model is also where the
//! polyglot vocabulary from the roadmap lives — pipeline, step, repo, route,
//! capability, boundary, error policy — and nothing domain-specific (no Odoo,
//! no Docker, no LangChain).

use std::collections::BTreeMap;

use serde_yaml::Value;

use crate::yamlx::WithMap;

/// Accepted schema versions. The canonical V1 tag is `pipecraft/v1`; the legacy
/// integer `1` (and string `"1"`) from the Python prototype are accepted and
/// normalised to `V1` so existing pipelines keep validating.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SchemaVersion {
    V1,
}

impl SchemaVersion {
    pub const CANONICAL_V1: &'static str = "pipecraft/v1";

    /// Parse from a raw YAML scalar. Returns `None` for unrecognised values so
    /// the caller can emit a coded error with the offending text.
    pub fn parse(value: &Value) -> Option<Self> {
        match value {
            Value::Number(n) if n.as_i64() == Some(1) => Some(Self::V1),
            Value::String(s) => match s.trim() {
                "pipecraft/v1" | "1" => Some(Self::V1),
                _ => None,
            },
            _ => None,
        }
    }
}

/// Global workspace settings. Pipelines stay independent YAML files; this only
/// says where to find them and which defaults to use.
#[derive(Debug, Clone)]
pub struct WorkspaceConfig {
    pub name: String,
    pub description: String,
    pub pipeline_dir: String,
    pub route_file: String,
    pub runs_dir: String,
    pub outputs_dir: String,
    pub default_pipeline: String,
    pub labels: WithMap,
}

impl Default for WorkspaceConfig {
    fn default() -> Self {
        Self {
            name: "default".into(),
            description: String::new(),
            pipeline_dir: ".pipelines/pipelines".into(),
            route_file: ".pipelines/routes.yaml".into(),
            runs_dir: ".pipelines/runs".into(),
            outputs_dir: ".pipelines/out".into(),
            default_pipeline: "solo-maintenance".into(),
            labels: WithMap::new(),
        }
    }
}

/// One declarative step. `step_type` selects the executor; executor-specific
/// config lives in `with` (and, for compatibility, a few promoted top-level
/// fields the prototype supported).
#[derive(Debug, Clone)]
pub struct PipelineStep {
    pub id: String,
    pub step_type: String,
    pub description: String,
    pub repo: String,
    /// Legacy convenience: `command: "..."` promoted to the top level.
    pub command: String,
    pub risk: String,
    pub requires_approval: bool,
    pub required: bool,
    /// Explicit DAG dependencies. Empty means "no declared deps".
    pub needs: Vec<String>,
    /// Dependency completion condition. Supported values: all_success,
    /// all_complete, any_failed, always.
    pub run_if: String,
    /// Named capabilities required/provided by this node. Capabilities are
    /// opaque strings to the core and therefore domain-agnostic.
    pub requires: Vec<String>,
    pub provides: Vec<String>,
    /// Runtime resources. Exclusive resources cannot overlap with any running
    /// user of the same resource; shared resources may overlap only with other
    /// shared users.
    pub exclusive_resources: Vec<String>,
    pub shared_resources: Vec<String>,
    /// A barrier runs alone: no other node may start while it is running and it
    /// waits for currently running nodes before it starts.
    pub barrier: bool,
    pub rules: WithMap,
    pub with: WithMap,
}

/// A parsed pipeline definition.
#[derive(Debug, Clone)]
pub struct PipelineDefinition {
    pub schema_version: SchemaVersion,
    pub name: String,
    pub description: String,
    pub context: WithMap,
    pub repos: WithMap,
    pub target: WithMap,
    pub targets: WithMap,
    pub inputs: WithMap,
    pub product_boundaries: WithMap,
    pub capabilities: WithMap,
    pub ui_rules: WithMap,
    pub steps: Vec<PipelineStep>,
    pub outputs: WithMap,
    pub on_error: ErrorPolicy,
    pub path: String,
}

/// `on_error` policy. Precedence when a required step fails:
/// `steps.<id>` → `types.<type>` → `statuses.<status>` → `default`.
#[derive(Debug, Clone)]
pub struct ErrorPolicy {
    pub default: String,
    pub steps: BTreeMap<String, String>,
    pub types: BTreeMap<String, String>,
    pub statuses: BTreeMap<String, String>,
}

impl Default for ErrorPolicy {
    fn default() -> Self {
        Self {
            default: "stop".into(),
            steps: BTreeMap::new(),
            types: BTreeMap::new(),
            statuses: BTreeMap::new(),
        }
    }
}

/// A label-based route from intent labels to a pipeline.
#[derive(Debug, Clone)]
pub struct RouteDefinition {
    pub id: String,
    pub pipeline: String,
    pub include: Vec<String>,
    pub any_of: Vec<String>,
    pub exclude: Vec<String>,
    pub description: String,
    pub set_context: WithMap,
}

/// A route that matched a given label set.
#[derive(Debug, Clone)]
pub struct RouteMatch {
    pub route: RouteDefinition,
    pub labels: Vec<String>,
    pub pipeline: String,
    pub context: WithMap,
}
