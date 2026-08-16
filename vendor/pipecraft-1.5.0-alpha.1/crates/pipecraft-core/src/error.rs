//! Structured, user-facing configuration errors with stable codes.
//!
//! Mirrors the Python prototype's `PipelineConfigError`: every error carries a
//! machine-stable `code`, an optional human `hint`, and optional structured
//! `details`. Codes are part of the contract — tooling and tests match on them,
//! so they must stay stable across releases within a major version.

use std::collections::BTreeMap;
use std::fmt;

/// A configuration / loading error surfaced before (or during) a run.
#[derive(Debug, Clone)]
pub struct ConfigError {
    pub message: String,
    pub code: String,
    pub hint: String,
    pub details: BTreeMap<String, String>,
}

impl ConfigError {
    pub fn new(message: impl Into<String>, code: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            code: code.into(),
            hint: String::new(),
            details: BTreeMap::new(),
        }
    }

    pub fn with_hint(mut self, hint: impl Into<String>) -> Self {
        self.hint = hint.into();
        self
    }

    pub fn with_detail(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.details.insert(key.into(), value.into());
        self
    }
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for ConfigError {}

pub type ConfigResult<T> = Result<T, ConfigError>;
