//! `pipecraft-core` — domain model, YAML loading, routing and static validation.
//!
//! This crate is the stable heart of PipeCraft. It knows about pipelines, steps,
//! repos, routes, capabilities, boundaries and error policy — and nothing about
//! how steps actually execute. It performs no process execution and has no
//! knowledge of any specific domain (Odoo, Docker, LangChain, ...).

pub mod error;
pub mod loader;
pub mod model;
pub mod routing;
pub mod validation;
pub mod yamlx;

pub use error::{ConfigError, ConfigResult};
pub use loader::{load_pipeline, load_workspace, PipelineCatalog};
pub use model::{
    ErrorPolicy, PipelineDefinition, PipelineStep, RouteDefinition, RouteMatch, SchemaVersion,
    WorkspaceConfig,
};
pub use routing::{extract_labels, load_routes, match_routes, select_route};
pub use validation::validate_static;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extract_labels_dedupes_and_strips_hash() {
        assert_eq!(extract_labels("feat: x #demo #fix #demo"), vec!["demo", "fix"]);
    }

    #[test]
    fn extract_labels_accepts_bare_tokens_when_no_hashes() {
        assert_eq!(extract_labels("ci build"), vec!["ci", "build"]);
    }

    #[test]
    fn extract_labels_ignores_mid_word_hash() {
        // `a#b` should not yield a label (preceded by a word char).
        assert!(extract_labels("color#fff").is_empty());
    }

    #[test]
    fn schema_version_accepts_legacy_and_canonical() {
        use serde_yaml::Value;
        assert_eq!(SchemaVersion::parse(&Value::from(1)), Some(SchemaVersion::V1));
        assert_eq!(
            SchemaVersion::parse(&Value::from("pipecraft/v1")),
            Some(SchemaVersion::V1)
        );
        assert_eq!(SchemaVersion::parse(&Value::from("v2")), None);
    }
}
