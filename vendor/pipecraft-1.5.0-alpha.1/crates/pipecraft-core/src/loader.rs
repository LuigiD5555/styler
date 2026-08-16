//! YAML loading into the stable domain model, with coded errors.
//!
//! Note vs. the Python prototype: `load_workspace` here reads
//! `.pipelines/workspace.yaml` (the path every example and test actually uses).
//! The prototype read `.me/workspace.yaml`, a leftover from another tool, so its
//! workspace config never loaded in practice and it always fell back to
//! defaults. This is the one intentional behavioural fix carried into V1.

use std::path::{Path, PathBuf};

use serde_yaml::Value;

use crate::error::{ConfigError, ConfigResult};
use crate::model::*;
use crate::yamlx::{self, get, WithMap};

/// Read a YAML file and require its root to be a mapping.
fn read_yaml(path: &Path) -> ConfigResult<Value> {
    let text = std::fs::read_to_string(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::InvalidData {
            ConfigError::new(
                format!("Could not read YAML as UTF-8: {}", path.display()),
                "YAML_ENCODING_ERROR",
            )
            .with_hint("Save the file as UTF-8.")
            .with_detail("path", path.display().to_string())
        } else {
            ConfigError::new(
                format!("Could not read file: {} ({e})", path.display()),
                "FILE_READ_ERROR",
            )
            .with_detail("path", path.display().to_string())
        }
    })?;

    let value: Value = serde_yaml::from_str(&text).map_err(|e| {
        ConfigError::new(
            format!("Invalid YAML syntax in {}: {e}", path.display()),
            "YAML_PARSE_ERROR",
        )
        .with_hint("Check indentation, list markers, quotes and mapping syntax.")
        .with_detail("path", path.display().to_string())
    })?;

    match value {
        Value::Null => Ok(Value::Mapping(Default::default())),
        Value::Mapping(_) => Ok(value),
        _ => Err(ConfigError::new(
            format!("YAML root must be a mapping: {}", path.display()),
            "YAML_ROOT_ERROR",
        )
        .with_hint("The top level of a PipeCraft YAML file must be key/value pairs.")
        .with_detail("path", path.display().to_string())),
    }
}

/// Load `.pipelines/workspace.yaml`, falling back to defaults if absent.
pub fn load_workspace(root: &Path) -> ConfigResult<WorkspaceConfig> {
    let path = root.join(".pipelines").join("workspace.yaml");
    if !path.exists() {
        return Ok(WorkspaceConfig::default());
    }
    let raw = read_yaml(&path)?;

    // `workspace:` block may be nested or flat at the root.
    let ws_block = get(&raw, "workspace").cloned().unwrap_or_else(|| raw.clone());
    let ws = yamlx::as_mapping(&ws_block, "workspace")?;
    let paths = get(&raw, "paths")
        .map(|v| yamlx::as_mapping(v, "paths"))
        .transpose()?
        .unwrap_or_default();
    let defaults = get(&raw, "defaults")
        .map(|v| yamlx::as_mapping(v, "defaults"))
        .transpose()?
        .unwrap_or_default();
    let labels = get(&raw, "labels")
        .map(|v| yamlx::as_mapping(v, "labels"))
        .transpose()?
        .unwrap_or_default();

    let pick = |m: &WithMap, key: &str, default: &str| -> String {
        m.get(key).and_then(yamlx::as_string).unwrap_or_else(|| default.to_string())
    };

    Ok(WorkspaceConfig {
        name: pick(&ws, "name", "default"),
        description: pick(&ws, "description", ""),
        pipeline_dir: pick(&paths, "pipeline_dir", ".pipelines/pipelines"),
        route_file: pick(&paths, "route_file", ".pipelines/routes.yaml"),
        runs_dir: pick(&paths, "runs_dir", ".pipelines/runs"),
        outputs_dir: pick(&paths, "outputs_dir", ".pipelines/out"),
        default_pipeline: pick(&defaults, "default_pipeline", "solo-maintenance"),
        labels,
    })
}

/// Load a single pipeline YAML file into the stable model.
pub fn load_pipeline(path: &Path) -> ConfigResult<PipelineDefinition> {
    let raw = read_yaml(path)?;

    let name = get(&raw, "name")
        .and_then(yamlx::as_string)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            ConfigError::new(
                format!("Pipeline is missing required field 'name': {}", path.display()),
                "MISSING_PIPELINE_NAME",
            )
            .with_hint("Add `name: your-pipeline-name` near the top of the file.")
            .with_detail("path", path.display().to_string())
        })?;

    // schema_version: accept `pipecraft/v1`, `1` (int) or `"1"`. Default to V1.
    let schema_version = match get(&raw, "schema_version") {
        None => SchemaVersion::V1,
        Some(v) => SchemaVersion::parse(v).ok_or_else(|| {
            ConfigError::new(
                format!("Unsupported schema_version in {}", path.display()),
                "SCHEMA_VERSION_ERROR",
            )
            .with_hint("Use `schema_version: pipecraft/v1` (legacy `1` is also accepted).")
        })?,
    };

    let steps = parse_steps(&raw, path)?;

    let map_field = |key: &str| -> ConfigResult<WithMap> {
        match get(&raw, key) {
            None => Ok(WithMap::new()),
            Some(v) => yamlx::as_mapping(v, key),
        }
    };

    let on_error = parse_error_policy(&raw)?;

    Ok(PipelineDefinition {
        schema_version,
        name,
        description: get(&raw, "description").and_then(yamlx::as_string).unwrap_or_default(),
        context: map_field("context")?,
        repos: map_field("repos")?,
        target: map_field("target")?,
        targets: map_field("targets")?,
        inputs: map_field("inputs")?,
        product_boundaries: map_field("product_boundaries")?,
        capabilities: map_field("capabilities")?,
        ui_rules: map_field("ui_rules")?,
        steps,
        outputs: map_field("outputs")?,
        on_error,
        path: path.display().to_string(),
    })
}

fn parse_steps(raw: &Value, path: &Path) -> ConfigResult<Vec<PipelineStep>> {
    let steps_value = get(raw, "steps").cloned().unwrap_or(Value::Null);
    let items = yamlx::as_list(&steps_value, "steps")?;
    let mut steps = Vec::with_capacity(items.len());

    for (i, item) in items.iter().enumerate() {
        let index = i + 1;
        let map = match item {
            Value::Mapping(_) => *item,
            _ => {
                return Err(ConfigError::new(
                    format!("Step #{index} in {} must be a mapping", path.display()),
                    "STEP_TYPE_ERROR",
                )
                .with_hint("Each step must be a YAML mapping with at least `id` and `type`."))
            }
        };

        let sid = get(map, "id")
            .and_then(yamlx::as_string)
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| format!("step_{index}"));

        let stype = get(map, "type").and_then(yamlx::as_string).filter(|s| !s.is_empty()).ok_or_else(|| {
            ConfigError::new(
                format!("Step {sid} in {} is missing required field 'type'", path.display()),
                "MISSING_STEP_TYPE",
            )
            .with_hint("Add `type: note`, `type: command`, `type: file_check`, etc.")
        })?;

        let rules = get(map, "rules")
            .map(|v| yamlx::as_mapping(v, &format!("steps.{sid}.rules")))
            .transpose()?
            .unwrap_or_default();
        let with = get(map, "with")
            .map(|v| yamlx::as_mapping(v, &format!("steps.{sid}.with")))
            .transpose()?
            .unwrap_or_default();
        let needs = get(map, "needs")
            .map(|v| yamlx::as_string_list(v, &format!("steps.{sid}.needs")))
            .transpose()?
            .unwrap_or_default();
        let requires = get(map, "requires")
            .map(|v| yamlx::as_string_list(v, &format!("steps.{sid}.requires")))
            .transpose()?
            .unwrap_or_default();
        let provides = get(map, "provides")
            .map(|v| yamlx::as_string_list(v, &format!("steps.{sid}.provides")))
            .transpose()?
            .unwrap_or_default();
        let exclusive_resources = get(map, "exclusive_resources")
            .map(|v| yamlx::as_string_list(v, &format!("steps.{sid}.exclusive_resources")))
            .transpose()?
            .unwrap_or_default();
        let shared_resources = get(map, "shared_resources")
            .map(|v| yamlx::as_string_list(v, &format!("steps.{sid}.shared_resources")))
            .transpose()?
            .unwrap_or_default();

        let requires_approval = match get(map, "requires_approval") {
            None => false,
            Some(v) => yamlx::as_bool(v, &format!("steps.{sid}.requires_approval"))?,
        };
        let required = match get(map, "required") {
            None => true,
            Some(v) => yamlx::as_bool(v, &format!("steps.{sid}.required"))?,
        };

        steps.push(PipelineStep {
            id: sid,
            step_type: stype,
            description: get(map, "description").and_then(yamlx::as_string).unwrap_or_default(),
            repo: get(map, "repo").and_then(yamlx::as_string).unwrap_or_default(),
            command: get(map, "command").and_then(yamlx::as_string).unwrap_or_default(),
            risk: get(map, "risk").and_then(yamlx::as_string).unwrap_or_else(|| "low".into()),
            requires_approval,
            required,
            needs,
            run_if: get(map, "run_if").and_then(yamlx::as_string).unwrap_or_else(|| "all_success".into()),
            requires,
            provides,
            exclusive_resources,
            shared_resources,
            barrier: match get(map, "barrier") {
                None => false,
                Some(v) => yamlx::as_bool(v, &format!("steps.{sid}.barrier"))?,
            },
            rules,
            with,
        });
    }
    Ok(steps)
}

fn parse_error_policy(raw: &Value) -> ConfigResult<ErrorPolicy> {
    let block = match get(raw, "on_error") {
        None => return Ok(ErrorPolicy::default()),
        Some(v) => yamlx::as_mapping(v, "on_error")?,
    };
    let mut policy = ErrorPolicy::default();
    if let Some(d) = block.get("default").and_then(yamlx::as_string) {
        policy.default = d;
    }
    let collect = |key: &str| -> std::collections::BTreeMap<String, String> {
        let mut out = std::collections::BTreeMap::new();
        if let Some(Value::Mapping(m)) = block.get(key) {
            for (k, v) in m {
                if let (Some(k), Some(v)) = (yamlx::as_string(k), yamlx::as_string(v)) {
                    out.insert(k, v);
                }
            }
        }
        out
    };
    policy.steps = collect("steps");
    policy.types = collect("types");
    policy.statuses = collect("statuses");
    Ok(policy)
}

/// Find and load pipelines from the configured pipeline directory.
pub struct PipelineCatalog {
    pub root: PathBuf,
    pub workspace: WorkspaceConfig,
    pub pipeline_dir: PathBuf,
}

impl PipelineCatalog {
    pub fn new(root: &Path, workspace: WorkspaceConfig) -> Self {
        let pipeline_dir = root.join(&workspace.pipeline_dir);
        Self { root: root.to_path_buf(), workspace, pipeline_dir }
    }

    /// Convenience constructor that loads the workspace itself.
    pub fn open(root: &Path) -> ConfigResult<Self> {
        let ws = load_workspace(root)?;
        Ok(Self::new(root, ws))
    }

    /// List pipeline *names*, falling back to file stems for unparseable files.
    pub fn list(&self) -> Vec<String> {
        if !self.pipeline_dir.exists() {
            return Vec::new();
        }
        let mut paths: Vec<PathBuf> = std::fs::read_dir(&self.pipeline_dir)
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .filter(|p| matches!(p.extension().and_then(|e| e.to_str()), Some("yaml") | Some("yml")))
            .collect();
        paths.sort();
        paths
            .into_iter()
            .map(|p| match load_pipeline(&p) {
                Ok(def) => def.name,
                Err(_) => p.file_stem().and_then(|s| s.to_str()).unwrap_or("?").to_string(),
            })
            .collect()
    }

    pub fn path_for(&self, name: &str) -> ConfigResult<PathBuf> {
        for suffix in [".yaml", ".yml"] {
            let p = self.pipeline_dir.join(format!("{name}{suffix}"));
            if p.exists() {
                return Ok(p);
            }
        }
        let direct = PathBuf::from(name);
        if direct.exists() {
            return Ok(direct);
        }
        Err(ConfigError::new(format!("Pipeline not found: {name}"), "PIPELINE_NOT_FOUND")
            .with_hint("Run `pipecraft list` to see available pipelines."))
    }

    pub fn load(&self, name: &str) -> ConfigResult<PipelineDefinition> {
        load_pipeline(&self.path_for(name)?)
    }
}
