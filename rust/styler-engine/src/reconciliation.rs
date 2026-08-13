use crate::error::EngineError;
use crate::protocol::{PlanResult, PlanStep, TargetRequestOutput};
use crate::registry::{self, InstallationRecord};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::Command;
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, Serialize)]
pub struct ReconciliationReport {
    pub registry_path: String,
    pub checked: usize,
    pub healthy: usize,
    pub drifted: usize,
    pub removed: usize,
    pub external: usize,
    pub unverifiable: usize,
    pub results: Vec<ReconciliationResult>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ReconciliationResult {
    pub record_id: String,
    pub component_id: String,
    pub provider: String,
    pub installation_kind: String,
    pub status: String,
    pub expected_version: String,
    pub actual_version: String,
    pub evidence: Value,
    pub repairable: bool,
    pub message: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AdoptionRequest {
    pub component_id: String,
    pub provider: String,
    pub installation_kind: String,
    #[serde(default)]
    pub package_id: String,
    #[serde(default = "default_scope")]
    pub scope: String,
    #[serde(default)]
    pub destination: String,
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub checksum_sha256: String,
    #[serde(default)]
    pub metadata: Value,
}

fn default_scope() -> String { "system".to_string() }

pub fn reconcile(path: &Path) -> Result<ReconciliationReport, EngineError> {
    let snapshot = registry::snapshot(path)?;
    let mut report = ReconciliationReport {
        registry_path: path.display().to_string(),
        checked: 0,
        healthy: 0,
        drifted: 0,
        removed: 0,
        external: 0,
        unverifiable: 0,
        results: Vec::new(),
    };
    for record in snapshot.records {
        if record.status == "removed" {
            report.removed += 1;
            continue;
        }
        if record.ownership == "external_detected" {
            report.external += 1;
        }
        let result = reconcile_record(&record);
        report.checked += 1;
        match result.status.as_str() {
            "healthy" => report.healthy += 1,
            "unverifiable" | "provider_unavailable" => report.unverifiable += 1,
            _ => report.drifted += 1,
        }
        report.results.push(result);
    }
    Ok(report)
}

pub fn reconcile_one(path: &Path, record_id: &str) -> Result<ReconciliationResult, EngineError> {
    let record = registry::find(path, record_id)?;
    Ok(reconcile_record(&record))
}

pub fn repair_plan(path: &Path, record_id: &str) -> Result<PlanResult, EngineError> {
    let record = registry::find(path, record_id)?;
    let state = reconcile_record(&record);
    if state.status == "healthy" {
        return Err(EngineError::InvalidRequest("el registro está saludable; no necesita reparación".to_string()));
    }
    if !state.repairable {
        return Err(EngineError::InvalidRequest(format!("el drift '{}' no tiene reparación automática segura", state.status)));
    }
    let (step_type, config) = reinstall_step(&record)?;
    let step = PlanStep {
        id: format!("repair:{}", record.record_id),
        step_type: step_type.to_string(),
        description: format!("Reparar {} a partir de su recibo", record.component_id),
        needs: Vec::new(), requires: Vec::new(), provides: Vec::new(),
        exclusive_resources: vec![format!("install:{}", record.component_id)], shared_resources: Vec::new(),
        criticality: "high".to_string(), stage: "repair".to_string(), required: true,
        provider: record.provider.clone(), config,
        rollback: json!({"source_record":record.record_id,"previous_state":state.status}),
    };
    Ok(PlanResult {
        name: format!("repair-{}", record.component_id),
        target: TargetRequestOutput { family: String::new(), architecture: String::new(), home: std::env::var("HOME").unwrap_or_default() },
        selected_components: vec![record.component_id.clone()], selected_providers: BTreeMap::new(),
        satisfied_capabilities: Vec::new(), missing_capabilities: Vec::new(), decisions: Vec::new(),
        order: vec![step.id.clone()], steps: vec![step], issues: Vec::new(), executable: true,
    })
}

pub fn adoption_preview(request: AdoptionRequest) -> Result<Value, EngineError> {
    let record = registry::external_record(
        &request.component_id, &request.provider, &request.installation_kind,
        &request.package_id, &request.scope, &request.destination, &request.source,
        &request.checksum_sha256, request.metadata,
    );
    let state = reconcile_record(&record);
    Ok(json!({
        "adoptable": matches!(state.status.as_str(), "healthy" | "version_changed" | "content_modified"),
        "ownership": "external_detected",
        "rollback_available": false,
        "record": record,
        "reconciliation": state,
        "warning": "adoptar sólo registra evidencia; no convierte la instalación externa en reversible"
    }))
}

pub fn adopt(path: &Path, request: AdoptionRequest) -> Result<InstallationRecord, EngineError> {
    let record = registry::external_record(
        &request.component_id, &request.provider, &request.installation_kind,
        &request.package_id, &request.scope, &request.destination, &request.source,
        &request.checksum_sha256, request.metadata,
    );
    let state = reconcile_record(&record);
    if !matches!(state.status.as_str(), "healthy" | "version_changed" | "content_modified") {
        return Err(EngineError::InvalidRequest(format!("no se puede adoptar: {}", state.message)));
    }
    registry::append_external(path, record.clone())?;
    Ok(record)
}

fn reconcile_record(record: &InstallationRecord) -> ReconciliationResult {
    let expected_version = expected_version(record);
    let mut result = match record.installation_kind.as_str() {
        "apt" => package_query(record, "dpkg-query", &["-W", "-f=${Status}\t${Version}", &record.package_id], parse_dpkg),
        "pacman" => package_query(record, "pacman", &["-Q", &record.package_id], parse_last_token),
        "dnf" | "zypper" => package_query(record, "rpm", &["-q", "--qf", "%{VERSION}-%{RELEASE}", &record.package_id], parse_trimmed),
        "snap" => package_query(record, "snap", &["list", &record.package_id], parse_snap),
        "flatpak" => flatpak_query(record),
        "artifact" => artifact_query(record),
        _ => base(record, "unverifiable", "tipo de instalación desconocido", false),
    };
    result.expected_version = expected_version.clone();
    if result.status == "healthy" && !expected_version.is_empty() && !result.actual_version.is_empty() && expected_version != result.actual_version {
        result.status = "version_changed".to_string();
        result.message = format!("versión registrada {expected_version}; versión actual {}", result.actual_version);
        result.repairable = record.ownership == "styler_managed";
    }
    result
}

fn package_query(record: &InstallationRecord, program: &str, args: &[&str], parser: fn(&str)->Option<String>) -> ReconciliationResult {
    if record.package_id.trim().is_empty() { return base(record, "unverifiable", "el recibo no contiene package_id", false); }
    if !safe_identifier(&record.package_id) { return base(record, "unverifiable", "package_id inseguro o inválido", false); }
    if !command_exists(program) { return base(record, "provider_unavailable", &format!("no se encontró {program}"), false); }
    match Command::new(program).args(args).output() {
        Ok(output) if output.status.success() => {
            let text = String::from_utf8_lossy(&output.stdout);
            match parser(&text) {
                Some(version) => {
                    let mut r = base(record, "healthy", "instalación presente", false);
                    r.actual_version = version;
                    r.evidence = json!({"program":program,"package_id":record.package_id,"exit_code":output.status.code()});
                    r
                }
                None => {
                    let mut r=base(record,"externally_removed","el gestor no confirmó una instalación activa",record.ownership=="styler_managed");
                    r.evidence=json!({"program":program,"stdout":text.trim()});
                    r
                }
            }
        }
        Ok(output) => { let mut r=base(record,"externally_removed","el gestor ya no reporta la instalación",record.ownership=="styler_managed"); r.evidence=json!({"program":program,"exit_code":output.status.code(),"stderr":String::from_utf8_lossy(&output.stderr).trim()}); r }
        Err(error) => base(record, "unverifiable", &format!("no fue posible consultar {program}: {error}"), false),
    }
}

fn flatpak_query(record: &InstallationRecord) -> ReconciliationResult {
    if record.package_id.trim().is_empty() { return base(record,"unverifiable","el recibo no contiene application_id",false); }
    if !safe_identifier(&record.package_id) { return base(record,"unverifiable","application_id inseguro o inválido",false); }
    if !command_exists("flatpak") { return base(record,"provider_unavailable","no se encontró flatpak",false); }
    let scope = if record.scope == "system" { "--system" } else { "--user" };
    package_query(record,"flatpak",&["info",scope,"--show-version",&record.package_id],parse_trimmed)
}

fn artifact_query(record: &InstallationRecord) -> ReconciliationResult {
    if record.destination.trim().is_empty() { return base(record,"unverifiable","el recibo no contiene destino",false); }
    let destination=Path::new(&record.destination);
    let managed: Vec<PathBuf> = if record.managed_paths.is_empty(){vec![destination.to_path_buf()]}else{record.managed_paths.iter().map(PathBuf::from).collect()};
    let present=managed.iter().filter(|p|p.exists()).count();
    if present==0 { let mut r=base(record,"externally_removed","ninguna ruta administrada existe",record.ownership=="styler_managed"); r.evidence=json!({"managed_paths":record.managed_paths}); return r; }
    if present<managed.len() { let mut r=base(record,"partially_present","sólo una parte de las rutas administradas existe",record.ownership=="styler_managed"); r.evidence=json!({"present":present,"expected":managed.len()}); return r; }
    if destination.is_file() && !record.checksum_sha256.is_empty() {
        match sha256_file(destination) {
            Ok((checksum,size)) if checksum.eq_ignore_ascii_case(&record.checksum_sha256) => { let mut r=base(record,"healthy","archivo administrado presente y checksum SHA-256 válido",false); r.evidence=json!({"path":record.destination,"checksum":checksum,"size":size}); r }
            Ok((checksum,_)) => { let mut r=base(record,"content_modified","el contenido ya no coincide con el checksum SHA-256 registrado",record.ownership=="styler_managed"); r.evidence=json!({"path":record.destination,"expected":record.checksum_sha256,"actual":checksum}); r }
            Err(error) => base(record,"unverifiable",&format!("no fue posible verificar el archivo: {error}"),false),
        }
    } else { let mut r=base(record,"healthy","rutas administradas presentes",false); r.evidence=json!({"managed_paths":record.managed_paths}); r }
}

fn sha256_file(path:&Path)->Result<(String,u64),EngineError>{
    let mut file=File::open(path).map_err(|e|EngineError::io(path,e))?;
    let mut hasher=Sha256::new();
    let mut buffer=[0u8;1024*1024];
    let mut size=0u64;
    loop{let read=file.read(&mut buffer).map_err(|e|EngineError::io(path,e))?;if read==0{break;}hasher.update(&buffer[..read]);size+=read as u64;}
    Ok((hex::encode(hasher.finalize()),size))
}

fn reinstall_step(record:&InstallationRecord)->Result<(String,Value),EngineError>{
    let original=record.metadata.get("config").cloned().unwrap_or_else(||json!({}));
    match record.installation_kind.as_str(){
        "apt"=>Ok(("install_apt".to_string(),ensure_package(original,&record.package_id))),
        "pacman"=>Ok(("install_pacman".to_string(),ensure_package(original,&record.package_id))),
        "dnf"=>Ok(("install_rpm".to_string(),ensure_package(original,&record.package_id))),
        "zypper"=>Ok(("install_zypper".to_string(),ensure_package(original,&record.package_id))),
        "snap"=>Ok(("install_snap".to_string(),ensure_package(original,&record.package_id))),
        "flatpak"=>{let mut c=original;if let Some(o)=c.as_object_mut(){o.insert("application_id".to_string(),json!(record.package_id));o.insert("scope".to_string(),json!(record.scope));}Ok(("install_flatpak".to_string(),c))},
        "artifact"=>{let step=record.metadata.get("step_type").and_then(Value::as_str).unwrap_or("install_file");if record.source.is_empty()||record.checksum_sha256.is_empty(){return Err(EngineError::InvalidRequest("el recibo no conserva origen y checksum suficientes".to_string()));}Ok((step.to_string(),original))},
        other=>Err(EngineError::InvalidRequest(format!("tipo sin reparación: {other}")))
    }
}
fn ensure_package(mut value:Value,package:&str)->Value{if !value.is_object(){value=json!({});}if let Some(o)=value.as_object_mut(){o.insert("packages".to_string(),json!([package]));}value}
fn expected_version(record:&InstallationRecord)->String{record.metadata.get("version").and_then(Value::as_str).or_else(||record.metadata.get("config").and_then(|v|v.get("version")).and_then(Value::as_str)).unwrap_or("").to_string()}
fn base(record:&InstallationRecord,status:&str,message:&str,repairable:bool)->ReconciliationResult{ReconciliationResult{record_id:record.record_id.clone(),component_id:record.component_id.clone(),provider:record.provider.clone(),installation_kind:record.installation_kind.clone(),status:status.to_string(),expected_version:String::new(),actual_version:String::new(),evidence:json!({}),repairable,message:message.to_string()}}
fn safe_identifier(value:&str)->bool{let mut chars=value.chars();matches!(chars.next(),Some(c) if c.is_ascii_alphanumeric())&&chars.all(|c|c.is_ascii_alphanumeric()||matches!(c,'.'|'_'|'-'|'+'|':'|'@'))}
fn command_exists(program:&str)->bool{std::env::var_os("PATH").map(|paths|std::env::split_paths(&paths).any(|p|p.join(program).is_file())).unwrap_or(false)}
fn parse_dpkg(text:&str)->Option<String>{let mut p=text.trim().split('\t');let status=p.next()?;let version=p.next()?.trim();if status.contains("install ok installed"){Some(version.to_string())}else{None}}
fn parse_last_token(text:&str)->Option<String>{text.split_whitespace().last().map(str::to_string)}
fn parse_trimmed(text:&str)->Option<String>{let v=text.trim();if v.is_empty(){None}else{Some(v.to_string())}}
fn parse_snap(text:&str)->Option<String>{text.lines().nth(1).and_then(|line|line.split_whitespace().nth(1)).map(str::to_string)}

#[cfg(test)]
mod tests { use super::*; #[test] fn dpkg_parser(){assert_eq!(parse_dpkg("install ok installed\t1.2\n"),Some("1.2".to_string()));} #[test] fn missing_dpkg_is_not_installed(){assert_eq!(parse_dpkg("unknown ok not-installed\t"),None);} }
