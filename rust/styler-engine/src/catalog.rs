use crate::error::EngineError;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use walkdir::WalkDir;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ComponentDefinition {
    pub schema_version: u32,
    pub id: String,
    pub name: String,
    pub kind: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub requires: Vec<String>,
    #[serde(default)]
    pub optional_requires: Vec<String>,
    #[serde(default)]
    pub provides: Vec<String>,
    #[serde(default)]
    pub conflicts: Vec<String>,
    #[serde(default)]
    pub criticality: String,
    #[serde(default)]
    pub providers: BTreeMap<String, ProviderDefinition>,
    #[serde(default)]
    pub resources: ResourceDefinition,
    #[serde(default)]
    pub verification: VerificationDefinition,
    #[serde(default)]
    pub rollback: RollbackDefinition,
    #[serde(default)]
    pub compatibility: CompatibilityDefinition,
    #[serde(skip)]
    pub source_path: String,
}

impl ComponentDefinition {
    pub fn required(&self) -> bool {
        self.criticality != "optional"
    }
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct ProviderDefinition {
    #[serde(rename = "type", default)]
    pub provider_type: String,
    #[serde(default)]
    pub families: Vec<String>,
    #[serde(default)]
    pub packages: Vec<String>,
    #[serde(default)]
    pub application_id: String,
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub config_root: String,
    #[serde(default)]
    pub priority: i64,
    #[serde(default)]
    pub checksum_sha256: String,
    #[serde(default)]
    pub artifact_kind: String,
    #[serde(default)]
    pub destination: String,
    #[serde(default)]
    pub file_name: String,
    #[serde(default)]
    pub strip_components: usize,
    #[serde(default)]
    pub max_size_bytes: u64,
    #[serde(default)]
    pub desktop_entry: bool,
    #[serde(default)]
    pub executable_name: String,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct ResourceDefinition {
    #[serde(default)]
    pub exclusive: Vec<String>,
    #[serde(default)]
    pub shared: Vec<String>,
    #[serde(default)]
    pub paths: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct VerificationDefinition {
    #[serde(default)]
    pub checks: Vec<String>,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct RollbackDefinition {
    #[serde(default)]
    pub level: String,
    #[serde(default)]
    pub strategy: String,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct CompatibilityDefinition {
    #[serde(default)]
    pub wayland: String,
    #[serde(default)]
    pub xwayland: String,
    #[serde(default)]
    pub x11: String,
}

#[derive(Debug, Clone)]
pub struct Catalog {
    pub root: PathBuf,
    pub components: BTreeMap<String, ComponentDefinition>,
    pub duplicate_ids: BTreeSet<String>,
    pub orphan_files: Vec<String>,
    pub warnings: Vec<String>,
}

impl Catalog {
    pub fn providers_for(&self, capability: &str) -> Vec<&ComponentDefinition> {
        self.components
            .values()
            .filter(|component| component.provides.iter().any(|item| item == capability))
            .collect()
    }
}

#[derive(Debug, Deserialize)]
struct CatalogIndex {
    #[serde(default)]
    schema_version: u32,
    #[serde(default)]
    components: BTreeMap<String, String>,
}

pub fn load_catalog(root: &Path) -> Result<Catalog, EngineError> {
    if !root.is_dir() {
        return Err(EngineError::Catalog(format!(
            "no existe el directorio de catálogo: {}",
            root.display()
        )));
    }
    let root = root
        .canonicalize()
        .map_err(|error| EngineError::io(root, error))?;
    let index_path = root.join("index.toml");
    let mut components = BTreeMap::new();
    let duplicate_ids = BTreeSet::new();
    let mut warnings = Vec::new();
    let mut indexed_paths = BTreeSet::new();

    if index_path.is_file() {
        let content = fs::read_to_string(&index_path)
            .map_err(|error| EngineError::io(&index_path, error))?;
        let index: CatalogIndex = toml::from_str(&content).map_err(|error| EngineError::Toml {
            path: index_path.clone(),
            message: error.to_string(),
        })?;
        if index.schema_version != 1 {
            warnings.push(format!(
                "index.toml usa schema_version {}; esta iteración espera 1",
                index.schema_version
            ));
        }
        for (declared_id, relative) in index.components {
            let relative_path = Path::new(&relative);
            if relative_path.is_absolute() {
                return Err(EngineError::Catalog(format!(
                    "el índice usa una ruta absoluta para '{declared_id}': {relative}"
                )));
            }
            let candidate = root.join(relative_path);
            let resolved = candidate
                .canonicalize()
                .map_err(|error| EngineError::io(&candidate, error))?;
            if !resolved.starts_with(&root) {
                return Err(EngineError::Catalog(format!(
                    "la ruta de '{declared_id}' escapa de la raíz del catálogo: {relative}"
                )));
            }
            let component = parse_component_file(&resolved)?;
            if component.id != declared_id {
                return Err(EngineError::Catalog(format!(
                    "el índice declara '{declared_id}' pero {} define '{}'",
                    resolved.display(),
                    component.id
                )));
            }
            indexed_paths.insert(resolved.clone());
            components.insert(component.id.clone(), component);
        }
    } else {
        warnings.push(format!(
            "{} no tiene index.toml; se usó descubrimiento recursivo de compatibilidad",
            root.display()
        ));
        for entry in WalkDir::new(&root).follow_links(false).into_iter().filter_map(Result::ok) {
            let path = entry.path();
            if !entry.file_type().is_file()
                || path.extension().and_then(|value| value.to_str()) != Some("toml")
            {
                continue;
            }
            let component = parse_component_file(path)?;
            components.entry(component.id.clone()).or_insert(component);
        }
    }

    let mut orphan_files = Vec::new();
    if index_path.is_file() {
        for entry in WalkDir::new(&root).follow_links(false).into_iter().filter_map(Result::ok) {
            let path = entry.path();
            if !entry.file_type().is_file()
                || path == index_path
                || path.extension().and_then(|value| value.to_str()) != Some("toml")
            {
                continue;
            }
            let resolved = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
            if !indexed_paths.contains(&resolved) {
                orphan_files.push(path.to_string_lossy().to_string());
            }
        }
    }
    orphan_files.sort();

    Ok(Catalog {
        root,
        components,
        duplicate_ids,
        orphan_files,
        warnings,
    })
}

fn parse_component_file(path: &Path) -> Result<ComponentDefinition, EngineError> {
    let content = fs::read_to_string(path).map_err(|error| EngineError::io(path, error))?;
    let mut component: ComponentDefinition = toml::from_str(&content).map_err(|error| EngineError::Toml {
        path: path.to_path_buf(),
        message: error.to_string(),
    })?;
    component.source_path = path.to_string_lossy().to_string();
    Ok(component)
}
