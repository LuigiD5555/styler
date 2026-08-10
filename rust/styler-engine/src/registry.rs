use crate::error::EngineError;
use crate::protocol::{ExecutionEvent, PlanResult, PlanStep};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub const REGISTRY_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallationRecord {
    pub schema_version: u32,
    pub record_id: String,
    pub component_id: String,
    pub provider: String,
    pub installation_kind: String,
    pub package_id: String,
    pub scope: String,
    pub source: String,
    pub checksum_sha256: String,
    pub destination: String,
    pub rollback_path: String,
    pub managed_paths: Vec<String>,
    pub run_id: String,
    pub journal_path: String,
    pub installed_at_ms: u128,
    pub status: String,
    pub ownership: String,
    pub metadata: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RegistryEvent {
    pub schema_version: u32,
    pub sequence: u64,
    pub timestamp_ms: u128,
    pub event: String,
    pub record: InstallationRecord,
}

#[derive(Debug, Clone, Serialize)]
pub struct RegistrySnapshot {
    pub path: String,
    pub records: Vec<InstallationRecord>,
    pub active: usize,
    pub removed: usize,
    pub corrupt_lines: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct RegistryAudit {
    pub path: String,
    pub checked: usize,
    pub healthy: usize,
    pub missing: usize,
    pub external: usize,
    pub issues: Vec<Value>,
}

pub fn default_registry_path() -> PathBuf {
    if let Ok(value) = std::env::var("STYLER_REGISTRY_PATH") {
        if !value.trim().is_empty() { return PathBuf::from(value); }
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home).join(".local/state/styler/installations.jsonl")
}

pub fn register_execution(plan: &PlanResult, journal_path: &Path, registry_path: &Path, run_id: &str) -> Result<Vec<InstallationRecord>, EngineError> {
    let events = read_journal(journal_path)?;
    let completed: BTreeSet<String> = events.iter().filter(|e| matches!(e.event.as_str(), "step_completed")).map(|e| e.step_id.clone()).collect();
    let artifact_receipts: BTreeMap<String, Value> = events.iter().filter(|e| e.event == "artifact_installed").map(|e| (e.step_id.clone(), e.data.clone())).collect();
    let mut records = Vec::new();
    for step in &plan.steps {
        if !completed.contains(&step.id) { continue; }
        if let Some(record) = record_for_step(step, artifact_receipts.get(&step.id), journal_path, run_id)? {
            append(registry_path, "installed", record.clone())?;
            records.push(record);
        }
    }
    Ok(records)
}

fn record_for_step(step: &PlanStep, receipt: Option<&Value>, journal_path: &Path, run_id: &str) -> Result<Option<InstallationRecord>, EngineError> {
    let installation_kind = match step.step_type.as_str() {
        "install_apt" => "apt", "install_pacman" => "pacman", "install_rpm" => "dnf", "install_zypper" => "zypper",
        "install_snap" => "snap", "install_flatpak" => "flatpak", "install_archive" | "overlay_install" | "install_appimage" | "install_file" => "artifact",
        _ => return Ok(None),
    }.to_string();
    let package_id = if installation_kind == "flatpak" {
        string_value(&step.config, "application_id")
    } else {
        step.config.get("packages").and_then(Value::as_array).and_then(|v| v.first()).and_then(Value::as_str).unwrap_or("").to_string()
    };
    let source = receipt.and_then(|v| v.get("source")).and_then(Value::as_str).unwrap_or_else(|| step.config.get("source").and_then(Value::as_str).unwrap_or("")).to_string();
    let checksum = receipt.and_then(|v| v.get("checksum_sha256")).and_then(Value::as_str).unwrap_or_else(|| step.config.get("checksum_sha256").and_then(Value::as_str).unwrap_or("")).to_string();
    let destination = receipt.and_then(|v| v.get("destination")).and_then(Value::as_str).unwrap_or_else(|| step.config.get("destination").and_then(Value::as_str).unwrap_or("")).to_string();
    let rollback_path = receipt.and_then(|v| v.get("rollback_path")).and_then(Value::as_str).unwrap_or("").to_string();
    let scope = step.config.get("scope").and_then(Value::as_str).unwrap_or(if installation_kind == "artifact" { "user" } else { "system" }).to_string();
    let component_id = step.id.split(':').nth(1).unwrap_or(&step.id).to_string();
    let installed_at_ms = now_ms();
    let record_id = stable_id(&format!("{component_id}|{}|{package_id}|{destination}|{run_id}", step.provider));
    let managed_paths = if destination.is_empty() { Vec::new() } else { vec![destination.clone()] };
    Ok(Some(InstallationRecord {
        schema_version: REGISTRY_SCHEMA_VERSION, record_id, component_id, provider: step.provider.clone(), installation_kind,
        package_id, scope, source, checksum_sha256: checksum, destination, rollback_path, managed_paths,
        run_id: run_id.to_string(), journal_path: journal_path.display().to_string(), installed_at_ms,
        status: "installed".to_string(), ownership: "styler_managed".to_string(), metadata: json!({"step_id": &step.id, "step_type": &step.step_type, "config": &step.config}),
    }))
}

pub fn snapshot(path: &Path) -> Result<RegistrySnapshot, EngineError> {
    let (records, corrupt_lines) = materialize(path)?;
    let removed = records.values().filter(|r| r.status == "removed").count();
    let active = records.len().saturating_sub(removed);
    Ok(RegistrySnapshot { path: path.display().to_string(), records: records.into_values().collect(), active, removed, corrupt_lines })
}

pub fn find(path: &Path, record_id: &str) -> Result<InstallationRecord, EngineError> {
    let mut records = materialize(path)?.0;
    records.remove(record_id).ok_or_else(|| EngineError::InvalidRequest(format!("registro no encontrado: {record_id}")))
}

pub fn audit(path: &Path) -> Result<RegistryAudit, EngineError> {
    let snap = snapshot(path)?; let mut healthy=0; let mut missing=0; let mut external=0; let mut issues=Vec::new();
    for record in &snap.records {
        if record.ownership != "styler_managed" { external += 1; continue; }
        if record.status == "removed" { continue; }
        if record.installation_kind == "artifact" {
            if record.destination.is_empty() || !Path::new(&record.destination).exists() { missing += 1; issues.push(json!({"record_id":record.record_id,"code":"MANAGED_PATH_MISSING","path":record.destination})); }
            else { healthy += 1; }
        } else { healthy += 1; }
    }
    Ok(RegistryAudit { path: snap.path, checked: snap.records.len(), healthy, missing, external, issues })
}

pub fn uninstall_plan(record: &InstallationRecord) -> Result<PlanResult, EngineError> {
    if record.ownership != "styler_managed" || record.status != "installed" { return Err(EngineError::InvalidRequest("sólo los registros activos administrados por Styler pueden desinstalarse".to_string())); }
    let (step_type, config) = match record.installation_kind.as_str() {
        "apt" => ("uninstall_apt", json!({"package":record.package_id,"record_id":record.record_id})),
        "pacman" => ("uninstall_pacman", json!({"package":record.package_id,"record_id":record.record_id})),
        "dnf" => ("uninstall_dnf", json!({"package":record.package_id,"record_id":record.record_id})),
        "zypper" => ("uninstall_zypper", json!({"package":record.package_id,"record_id":record.record_id})),
        "snap" => ("uninstall_snap", json!({"package":record.package_id,"record_id":record.record_id})),
        "flatpak" => ("uninstall_flatpak", json!({"application_id":record.package_id,"scope":record.scope,"record_id":record.record_id})),
        "artifact" => ("remove_managed_artifact", json!({"record_id":record.record_id,"destination":record.destination,"rollback_path":record.rollback_path,"managed_paths":record.managed_paths})),
        other => return Err(EngineError::InvalidRequest(format!("tipo sin desinstalador seguro: {other}"))),
    };
    let step = PlanStep { id: format!("uninstall:{}",record.record_id), step_type: step_type.to_string(), description: format!("Desinstalar {} usando evidencia registrada",record.component_id), needs:Vec::new(), requires:Vec::new(), provides:Vec::new(), exclusive_resources:vec![format!("install:{}",record.component_id)], shared_resources:Vec::new(), criticality:"high".to_string(), stage:"uninstall".to_string(), required:true, provider:record.provider.clone(), config, rollback:json!({"source_record":record.record_id}) };
    Ok(PlanResult { name:format!("uninstall-{}",record.component_id), target:crate::protocol::TargetRequestOutput{family:String::new(),architecture:String::new(),home:std::env::var("HOME").unwrap_or_default()}, selected_components:vec![record.component_id.clone()], selected_providers:BTreeMap::new(), satisfied_capabilities:Vec::new(), missing_capabilities:Vec::new(), decisions:Vec::new(), order:vec![step.id.clone()], steps:vec![step], issues:Vec::new(), executable:true })
}


pub fn external_record(component_id:&str, provider:&str, installation_kind:&str, package_id:&str, scope:&str, destination:&str, source:&str, checksum_sha256:&str, metadata:Value)->InstallationRecord{
    let installed_at_ms=now_ms();
    let record_id=stable_id(&format!("external|{component_id}|{provider}|{package_id}|{destination}"));
    InstallationRecord{schema_version:REGISTRY_SCHEMA_VERSION,record_id,component_id:component_id.to_string(),provider:provider.to_string(),installation_kind:installation_kind.to_string(),package_id:package_id.to_string(),scope:scope.to_string(),source:source.to_string(),checksum_sha256:checksum_sha256.to_string(),destination:destination.to_string(),rollback_path:String::new(),managed_paths:if destination.is_empty(){Vec::new()}else{vec![destination.to_string()]},run_id:String::new(),journal_path:String::new(),installed_at_ms,status:"installed".to_string(),ownership:"external_detected".to_string(),metadata}
}
pub fn append_external(path:&Path,record:InstallationRecord)->Result<(),EngineError>{append(path,"adopted_external",record)}

pub fn mark_removed(path:&Path, record_id:&str, run_id:&str)->Result<(),EngineError>{let mut record=find(path,record_id)?; record.status="removed".to_string(); record.run_id=run_id.to_string(); append(path,"removed",record)}

fn append(path:&Path,event:&str,record:InstallationRecord)->Result<(),EngineError>{if path.exists() && fs::symlink_metadata(path).map_err(|e|EngineError::io(path,e))?.file_type().is_symlink(){return Err(EngineError::InvalidRequest("el registro no puede ser un enlace simbólico".to_string()));} if let Some(parent)=path.parent(){fs::create_dir_all(parent).map_err(|e|EngineError::io(parent,e))?;} let sequence=next_sequence(path)?; let payload=RegistryEvent{schema_version:REGISTRY_SCHEMA_VERSION,sequence,timestamp_ms:now_ms(),event:event.to_string(),record}; let mut file=OpenOptions::new().create(true).append(true).open(path).map_err(|e|EngineError::io(path,e))?; serde_json::to_writer(&mut file,&payload)?; file.write_all(b"\n").map_err(|e|EngineError::io(path,e))?; file.sync_data().map_err(|e|EngineError::io(path,e))}
fn next_sequence(path:&Path)->Result<u64,EngineError>{if !path.exists(){return Ok(1);} let file=File::open(path).map_err(|e|EngineError::io(path,e))?; Ok(BufReader::new(file).lines().filter_map(Result::ok).filter_map(|l|serde_json::from_str::<RegistryEvent>(&l).ok()).map(|e|e.sequence).max().unwrap_or(0)+1)}
fn materialize(path:&Path)->Result<(BTreeMap<String,InstallationRecord>,usize),EngineError>{let mut map=BTreeMap::new();let mut corrupt=0;if !path.exists(){return Ok((map,0));}let file=File::open(path).map_err(|e|EngineError::io(path,e))?;for line in BufReader::new(file).lines(){match line{Ok(text)=>match serde_json::from_str::<RegistryEvent>(&text){Ok(event)=>{map.insert(event.record.record_id.clone(),event.record);},Err(_)=>corrupt+=1},Err(_)=>corrupt+=1}}Ok((map,corrupt))}
fn read_journal(path:&Path)->Result<Vec<ExecutionEvent>,EngineError>{let file=File::open(path).map_err(|e|EngineError::io(path,e))?;let mut out=Vec::new();for line in BufReader::new(file).lines(){let text=line.map_err(|e|EngineError::io(path,e))?;if let Ok(event)=serde_json::from_str::<ExecutionEvent>(&text){out.push(event);}}Ok(out)}
fn stable_id(value:&str)->String{let mut h=Sha256::new();h.update(value.as_bytes());hex::encode(h.finalize())[..24].to_string()}
fn now_ms()->u128{SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis()}
fn string_value(value:&Value,key:&str)->String{value.get(key).and_then(Value::as_str).unwrap_or("").to_string()}

#[cfg(test)] mod tests { use super::*; #[test] fn ids_are_stable(){assert_eq!(stable_id("a"),stable_id("a"));} }
