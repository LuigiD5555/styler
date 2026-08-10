use crate::error::EngineError;
use crate::protocol::{FileHash, ScanFailure, ScanResult};
use blake2::digest::{Update, VariableOutput};
use blake2::Blake2bVar;
use rayon::iter::{ParallelBridge, ParallelIterator};
use std::fs::{self, File};
use std::io::Read;
use std::path::Path;
use walkdir::WalkDir;

pub const HASH_ALGORITHM: &str = "blake2b-128";
const BUFFER_SIZE: usize = 1024 * 1024;

pub fn hash_file(path: &Path) -> Result<FileHash, EngineError> {
    let metadata = fs::symlink_metadata(path).map_err(|error| EngineError::io(path, error))?;
    if metadata.file_type().is_symlink() {
        return Err(EngineError::InvalidRequest(format!(
            "no se siguen enlaces simbólicos: {}",
            path.display()
        )));
    }
    if !metadata.is_file() {
        return Err(EngineError::InvalidRequest(format!(
            "la ruta no es un archivo regular: {}",
            path.display()
        )));
    }

    let mut file = File::open(path).map_err(|error| EngineError::io(path, error))?;
    let mut hasher = Blake2bVar::new(16)
        .map_err(|error| EngineError::InvalidRequest(format!("BLAKE2b no disponible: {error}")))?;
    let mut buffer = vec![0_u8; BUFFER_SIZE];
    let mut size = 0_u64;
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|error| EngineError::io(path, error))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
        size += read as u64;
    }
    let mut digest = [0_u8; 16];
    hasher
        .finalize_variable(&mut digest)
        .map_err(|error| EngineError::InvalidRequest(format!("falló BLAKE2b: {error}")))?;

    Ok(FileHash {
        path: path.to_string_lossy().to_string(),
        checksum: to_hex(&digest),
        size,
        algorithm: HASH_ALGORITHM,
    })
}

pub fn scan_paths(paths: &[String]) -> ScanResult {
    let mut outcomes: Vec<Result<FileHash, ScanFailure>> = Vec::new();
    for raw in paths {
        let root = Path::new(raw);
        let Ok(metadata) = fs::symlink_metadata(root) else {
            outcomes.push(Err(ScanFailure {
                path: raw.clone(),
                message: "la ruta no existe o no puede inspeccionarse".to_string(),
            }));
            continue;
        };
        if metadata.file_type().is_symlink() {
            continue;
        }
        if metadata.is_file() {
            outcomes.push(hash_outcome(root));
            continue;
        }
        if !metadata.is_dir() {
            continue;
        }
        let mut root_outcomes: Vec<Result<FileHash, ScanFailure>> = WalkDir::new(root)
            .follow_links(false)
            .into_iter()
            .filter_map(Result::ok)
            .filter(|entry| entry.file_type().is_file() && !entry.file_type().is_symlink())
            .par_bridge()
            .map(|entry| hash_outcome(entry.path()))
            .collect();
        outcomes.append(&mut root_outcomes);
    }

    outcomes.sort_by(|left, right| outcome_path(left).cmp(outcome_path(right)));
    outcomes.dedup_by(|left, right| outcome_path(left) == outcome_path(right));
    let mut entries = Vec::new();
    let mut failures = Vec::new();
    let mut total_bytes = 0_u64;
    for outcome in outcomes {
        match outcome {
            Ok(entry) => {
                total_bytes += entry.size;
                entries.push(entry);
            }
            Err(failure) => failures.push(failure),
        }
    }
    ScanResult {
        scanned_files: entries.len(),
        total_bytes,
        entries,
        failures,
    }
}

fn hash_outcome(path: &Path) -> Result<FileHash, ScanFailure> {
    hash_file(path).map_err(|error| ScanFailure {
        path: path.to_string_lossy().to_string(),
        message: error.to_string(),
    })
}

fn outcome_path(outcome: &Result<FileHash, ScanFailure>) -> &str {
    match outcome {
        Ok(entry) => &entry.path,
        Err(failure) => &failure.path,
    }
}

fn to_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}

#[cfg(test)]
mod tests {
    use super::to_hex;

    #[test]
    fn renders_lowercase_hex() {
        assert_eq!(to_hex(&[0, 15, 16, 255]), "000f10ff");
    }
}
