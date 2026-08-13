use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const PROTOCOL_VERSION: u32 = 1;
pub const EVENT_PROTOCOL_VERSION: u32 = 1;
pub const ENGINE_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const EXECUTION_CONFIRMATION: &str = "I_UNDERSTAND_STYLER_WILL_CHANGE_MY_SYSTEM";

#[derive(Debug, Clone, Serialize)]
pub struct Envelope<T> {
    pub protocol_version: u32,
    pub engine_version: &'static str,
    pub ok: bool,
    pub result: Option<T>,
    pub error: Option<ErrorPayload>,
}

impl<T: Serialize> Envelope<T> {
    pub fn success(result: T) -> Self {
        Self {
            protocol_version: PROTOCOL_VERSION,
            engine_version: ENGINE_VERSION,
            ok: true,
            result: Some(result),
            error: None,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ErrorEnvelope {
    pub protocol_version: u32,
    pub engine_version: &'static str,
    pub ok: bool,
    pub result: Option<serde_json::Value>,
    pub error: Option<ErrorPayload>,
}

impl ErrorEnvelope {
    pub fn failure(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            protocol_version: PROTOCOL_VERSION,
            engine_version: ENGINE_VERSION,
            ok: false,
            result: None,
            error: Some(ErrorPayload {
                code: code.into(),
                message: message.into(),
            }),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ErrorPayload {
    pub code: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct VersionInfo {
    pub protocol_version: u32,
    pub event_protocol_version: u32,
    pub engine_version: &'static str,
    pub hash_algorithm: &'static str,
    pub execution_enabled: bool,
    pub execution_default_mode: &'static str,
    pub execution_confirmation: &'static str,
}

#[derive(Debug, Clone, Serialize)]
pub struct HostContext {
    pub home: String,
    pub os_id: String,
    pub os_name: String,
    pub os_version: String,
    pub family: String,
    pub architecture: String,
    pub desktop: String,
    pub session_type: String,
    pub package_managers: Vec<String>,
    pub tools: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct FileHash {
    pub path: String,
    pub checksum: String,
    pub size: u64,
    pub algorithm: &'static str,
}

#[derive(Debug, Clone, Serialize)]
pub struct ScanFailure {
    pub path: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ScanResult {
    pub entries: Vec<FileHash>,
    pub failures: Vec<ScanFailure>,
    pub scanned_files: usize,
    pub total_bytes: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PlanRequest {
    pub catalog_root: String,
    #[serde(default)]
    pub desired_components: Vec<String>,
    #[serde(default)]
    pub target: TargetRequest,
    #[serde(default)]
    pub provider_preferences: BTreeMap<String, String>,
    #[serde(default)]
    pub allowed_provider_types: Vec<String>,
    #[serde(default = "default_plan_name")]
    pub name: String,
}

fn default_plan_name() -> String {
    "styler-plan".to_string()
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct TargetRequest {
    #[serde(default)]
    pub family: String,
    #[serde(default)]
    pub architecture: String,
    #[serde(default)]
    pub home: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DiagnosticIssue {
    pub severity: String,
    pub code: String,
    pub component_id: String,
    pub path: String,
    pub field: String,
    pub message: String,
    pub suggestion: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DiagnosticReport {
    pub catalog_root: String,
    pub components: usize,
    pub errors: usize,
    pub warnings: usize,
    pub issues: Vec<DiagnosticIssue>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResolutionDecision {
    pub component_id: String,
    pub requirement: String,
    pub candidates: Vec<String>,
    pub chosen_component: String,
    pub chosen_provider: String,
    pub reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PlanIssue {
    pub severity: String,
    pub code: String,
    pub component_id: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PlanStep {
    pub id: String,
    pub step_type: String,
    pub description: String,
    pub needs: Vec<String>,
    pub requires: Vec<String>,
    pub provides: Vec<String>,
    pub exclusive_resources: Vec<String>,
    pub shared_resources: Vec<String>,
    pub criticality: String,
    pub stage: String,
    pub required: bool,
    pub provider: String,
    pub config: serde_json::Value,
    pub rollback: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PlanResult {
    pub name: String,
    pub target: TargetRequestOutput,
    pub selected_components: Vec<String>,
    pub selected_providers: BTreeMap<String, String>,
    pub satisfied_capabilities: Vec<String>,
    pub missing_capabilities: Vec<String>,
    pub decisions: Vec<ResolutionDecision>,
    pub order: Vec<String>,
    pub steps: Vec<PlanStep>,
    pub issues: Vec<PlanIssue>,
    pub executable: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TargetRequestOutput {
    pub family: String,
    pub architecture: String,
    pub home: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ExecutionRequest {
    pub plan: PlanResult,
    #[serde(default)]
    pub options: ExecutionOptions,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ExecutionOptions {
    #[serde(default = "default_execution_mode")]
    pub mode: String,
    #[serde(default)]
    pub journal_path: String,
    #[serde(default)]
    pub cancel_file: String,
    #[serde(default = "default_timeout_seconds")]
    pub default_timeout_seconds: u64,
    #[serde(default = "default_true")]
    pub continue_on_optional_failure: bool,
    #[serde(default = "default_elevation")]
    pub elevation: String,
    #[serde(default)]
    pub confirmation: String,
    #[serde(default)]
    pub environment: BTreeMap<String, String>,
    #[serde(default)]
    pub journal_output: bool,
    #[serde(default)]
    pub registry_path: String,
}

impl Default for ExecutionOptions {
    fn default() -> Self {
        Self {
            mode: default_execution_mode(),
            journal_path: String::new(),
            cancel_file: String::new(),
            default_timeout_seconds: default_timeout_seconds(),
            continue_on_optional_failure: true,
            elevation: default_elevation(),
            confirmation: String::new(),
            environment: BTreeMap::new(),
            journal_output: false,
            registry_path: String::new(),
        }
    }
}

fn default_execution_mode() -> String {
    "dry_run".to_string()
}

fn default_timeout_seconds() -> u64 {
    900
}

fn default_true() -> bool {
    true
}

fn default_elevation() -> String {
    "none".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecutionEvent {
    pub event_protocol_version: u32,
    pub engine_version: &'static str,
    pub sequence: u64,
    pub timestamp_ms: u128,
    pub run_id: String,
    pub event: String,
    pub step_id: String,
    pub message: String,
    pub data: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecutionSummary {
    pub run_id: String,
    pub status: String,
    pub mode: String,
    pub journal_path: String,
    pub started_at_ms: u128,
    pub ended_at_ms: u128,
    pub total_steps: usize,
    pub completed_steps: usize,
    pub failed_steps: usize,
    pub skipped_steps: usize,
    pub cancelled_steps: usize,
    pub unsupported_steps: usize,
    pub dry_run: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JournalSummary {
    pub path: String,
    pub run_id: String,
    pub status: String,
    pub events: usize,
    pub last_sequence: u64,
    pub completed_steps: Vec<String>,
    pub failed_steps: Vec<String>,
    pub skipped_steps: Vec<String>,
    pub cancelled_steps: Vec<String>,
    pub unsupported_steps: Vec<String>,
    pub in_flight_steps: Vec<String>,
    pub truncated_lines: usize,
}
