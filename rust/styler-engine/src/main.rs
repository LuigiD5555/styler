mod artifact;
mod catalog;
mod diagnostics;
mod error;
mod execution;
mod hashing;
mod host;
mod journal;
mod planner;
mod protocol;
mod registry;
mod reconciliation;

use crate::catalog::load_catalog;
use crate::error::EngineError;
use crate::hashing::{hash_file, scan_paths, HASH_ALGORITHM};
use crate::protocol::{
    Envelope, ErrorEnvelope, ExecutionRequest, PlanRequest, VersionInfo,
    EVENT_PROTOCOL_VERSION, EXECUTION_CONFIRMATION,
};
use std::env;
use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

fn main() -> ExitCode {
    match run(env::args().skip(1).collect()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            let payload = ErrorEnvelope::failure("ENGINE_ERROR", error.to_string());
            eprintln!(
                "{}",
                serde_json::to_string(&payload).unwrap_or_else(|_| "{\"ok\":false}".to_string())
            );
            ExitCode::from(2)
        }
    }
}

fn run(args: Vec<String>) -> Result<(), EngineError> {
    let Some(command) = args.first().map(String::as_str) else {
        return Err(EngineError::InvalidRequest(help()));
    };
    match command {
        "version" => write_json(&Envelope::success(VersionInfo {
            protocol_version: protocol::PROTOCOL_VERSION,
            event_protocol_version: EVENT_PROTOCOL_VERSION,
            engine_version: protocol::ENGINE_VERSION,
            hash_algorithm: HASH_ALGORITHM,
            execution_enabled: true,
            execution_default_mode: "dry_run",
            execution_confirmation: EXECUTION_CONFIRMATION,
        })),
        "host" => write_json(&Envelope::success(host::detect_host())),
        "hash-file" => {
            let path = required_arg(&args, 1, "hash-file requiere una ruta")?;
            write_json(&Envelope::success(hash_file(Path::new(path))?))
        }
        "scan" => {
            if args.len() < 2 {
                return Err(EngineError::InvalidRequest(
                    "scan requiere una o más rutas".to_string(),
                ));
            }
            write_json(&Envelope::success(scan_paths(&args[1..])))
        }
        "diagnose" => {
            let root = required_arg(&args, 1, "diagnose requiere la raíz del catálogo")?;
            let catalog = load_catalog(Path::new(root))?;
            write_json(&Envelope::success(diagnostics::diagnose(&catalog)))
        }
        "plan" => {
            let request_path = args.get(1).map(String::as_str).unwrap_or("-");
            let request: PlanRequest = serde_json::from_str(&read_request(request_path)?)?;
            let catalog = load_catalog(Path::new(&request.catalog_root))?;
            write_json(&Envelope::success(planner::build_plan(&catalog, request)))
        }
        "execute" => {
            let request_path = args.get(1).map(String::as_str).unwrap_or("-");
            let request: ExecutionRequest = serde_json::from_str(&read_request(request_path)?)?;
            let stdout = io::stdout();
            let mut output = stdout.lock();
            execution::execute(request, &mut output)?;
            output.flush().map_err(EngineError::io_without_path)
        }
        "journal-summary" => {
            let path = required_arg(&args, 1, "journal-summary requiere una ruta")?;
            write_json(&Envelope::success(journal::summarize(Path::new(path))?))
        }
        "registry-list" => {
            let path = args.get(1).map(PathBuf::from).unwrap_or_else(registry::default_registry_path);
            write_json(&Envelope::success(registry::snapshot(&path)?))
        }
        "registry-show" => {
            let record_id = required_arg(&args, 1, "registry-show requiere record_id")?;
            let path = args.get(2).map(PathBuf::from).unwrap_or_else(registry::default_registry_path);
            write_json(&Envelope::success(registry::find(&path, record_id)?))
        }
        "registry-audit" => {
            let path = args.get(1).map(PathBuf::from).unwrap_or_else(registry::default_registry_path);
            write_json(&Envelope::success(registry::audit(&path)?))
        }
        "uninstall-plan" => {
            let record_id = required_arg(&args, 1, "uninstall-plan requiere record_id")?;
            let path = args.get(2).map(PathBuf::from).unwrap_or_else(registry::default_registry_path);
            let record = registry::find(&path, record_id)?;
            write_json(&Envelope::success(registry::uninstall_plan(&record)?))
        }
        "reconcile" => {
            let path = args.get(1).map(PathBuf::from).unwrap_or_else(registry::default_registry_path);
            write_json(&Envelope::success(reconciliation::reconcile(&path)?))
        }
        "reconcile-show" => {
            let record_id = required_arg(&args, 1, "reconcile-show requiere record_id")?;
            let path = args.get(2).map(PathBuf::from).unwrap_or_else(registry::default_registry_path);
            write_json(&Envelope::success(reconciliation::reconcile_one(&path, record_id)?))
        }
        "repair-plan" => {
            let record_id = required_arg(&args, 1, "repair-plan requiere record_id")?;
            let path = args.get(2).map(PathBuf::from).unwrap_or_else(registry::default_registry_path);
            write_json(&Envelope::success(reconciliation::repair_plan(&path, record_id)?))
        }
        "adoption-preview" => {
            let request_path = args.get(1).map(String::as_str).unwrap_or("-");
            let request: reconciliation::AdoptionRequest = serde_json::from_str(&read_request(request_path)?)?;
            write_json(&Envelope::success(reconciliation::adoption_preview(request)?))
        }
        "registry-adopt" => {
            let request_path = args.get(1).map(String::as_str).unwrap_or("-");
            let request: reconciliation::AdoptionRequest = serde_json::from_str(&read_request(request_path)?)?;
            let path = args.get(2).map(PathBuf::from).unwrap_or_else(registry::default_registry_path);
            write_json(&Envelope::success(reconciliation::adopt(&path, request)?))
        }
        "help" | "--help" | "-h" => {
            println!("{}", help());
            Ok(())
        }
        unknown => Err(EngineError::InvalidRequest(format!(
            "comando desconocido '{unknown}'\n{}",
            help()
        ))),
    }
}

fn required_arg<'a>(args: &'a [String], index: usize, message: &str) -> Result<&'a str, EngineError> {
    args.get(index)
        .map(String::as_str)
        .ok_or_else(|| EngineError::InvalidRequest(message.to_string()))
}

fn read_request(path: &str) -> Result<String, EngineError> {
    if path == "-" {
        let mut input = String::new();
        io::stdin()
            .read_to_string(&mut input)
            .map_err(EngineError::io_without_path)?;
        return Ok(input);
    }
    fs::read_to_string(PathBuf::from(path)).map_err(|error| EngineError::io(path, error))
}

fn write_json<T: serde::Serialize>(value: &T) -> Result<(), EngineError> {
    let output = serde_json::to_string_pretty(value)?;
    println!("{output}");
    Ok(())
}

fn help() -> String {
    [
        "Styler Engine 0.5 — reconciliación, drift y reparación verificable",
        "",
        "Uso:",
        "  styler-engine version",
        "  styler-engine host",
        "  styler-engine hash-file RUTA",
        "  styler-engine scan RUTA [RUTA...]",
        "  styler-engine diagnose RUTA_CATALOGO",
        "  styler-engine plan REQUEST.json",
        "  styler-engine execute EXECUTION_REQUEST.json",
        "  styler-engine journal-summary JOURNAL.jsonl",
        "  styler-engine registry-list [REGISTRY.jsonl]",
        "  styler-engine registry-show RECORD_ID [REGISTRY.jsonl]",
        "  styler-engine registry-audit [REGISTRY.jsonl]",
        "  styler-engine uninstall-plan RECORD_ID [REGISTRY.jsonl]",
        "  styler-engine reconcile [REGISTRY.jsonl]",
        "  styler-engine reconcile-show RECORD_ID [REGISTRY.jsonl]",
        "  styler-engine repair-plan RECORD_ID [REGISTRY.jsonl]",
        "  styler-engine adoption-preview REQUEST.json",
        "  styler-engine registry-adopt REQUEST.json [REGISTRY.jsonl]",
        "  cat REQUEST.json | styler-engine plan -",
        "  cat EXECUTION_REQUEST.json | styler-engine execute -",
        "",
        "execute emite eventos JSONL. El modo predeterminado es dry_run.",
        "El modo apply requiere STYLER_ENABLE_EXECUTION=1 y confirmación explícita.",
    ]
    .join("\n")
}
