use std::fmt::{Display, Formatter};
use std::path::PathBuf;

#[derive(Debug)]
pub enum EngineError {
    Io { path: Option<PathBuf>, message: String },
    Json(String),
    Toml { path: PathBuf, message: String },
    InvalidRequest(String),
    Catalog(String),
}

impl EngineError {
    pub fn io(path: impl Into<PathBuf>, error: impl Display) -> Self {
        Self::Io {
            path: Some(path.into()),
            message: error.to_string(),
        }
    }

    pub fn io_without_path(error: impl Display) -> Self {
        Self::Io {
            path: None,
            message: error.to_string(),
        }
    }
}

impl Display for EngineError {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io { path: Some(path), message } => {
                write!(f, "error de E/S en {}: {message}", path.display())
            }
            Self::Io { path: None, message } => write!(f, "error de E/S: {message}"),
            Self::Json(message) => write!(f, "JSON inválido: {message}"),
            Self::Toml { path, message } => {
                write!(f, "TOML inválido en {}: {message}", path.display())
            }
            Self::InvalidRequest(message) => write!(f, "solicitud inválida: {message}"),
            Self::Catalog(message) => write!(f, "catálogo inválido: {message}"),
        }
    }
}

impl std::error::Error for EngineError {}

impl From<serde_json::Error> for EngineError {
    fn from(value: serde_json::Error) -> Self {
        Self::Json(value.to_string())
    }
}
