// Extensión opcional de hashing para Styler.
//
// Contrato crítico: debe producir exactamente BLAKE2b con digest_size=16,
// igual que hashlib.blake2b(digest_size=16) en el fallback Python. La versión
// anterior usaba BLAKE3 y podía generar IDs incompatibles para el object store.

use blake2::digest::{Update, VariableOutput};
use blake2::Blake2bVar;
use pyo3::prelude::*;
use rayon::prelude::*;
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use walkdir::WalkDir;

const BUFFER_SIZE: usize = 1024 * 1024;

fn to_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}

fn hash_path(path: &Path) -> Option<(String, String, u64)> {
    let metadata = fs::symlink_metadata(path).ok()?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return None;
    }
    let mut file = File::open(path).ok()?;
    let mut hasher = Blake2bVar::new(16).ok()?;
    let mut buffer = vec![0_u8; BUFFER_SIZE];
    let mut size = 0_u64;
    loop {
        let read = file.read(&mut buffer).ok()?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
        size += read as u64;
    }
    let mut digest = [0_u8; 16];
    hasher.finalize_variable(&mut digest).ok()?;
    Some((
        path.to_string_lossy().to_string(),
        to_hex(&digest),
        size,
    ))
}

#[pyfunction]
fn hash_file(path: String) -> PyResult<Option<(String, u64)>> {
    Ok(hash_path(Path::new(&path)).map(|(_, checksum, size)| (checksum, size)))
}

#[pyfunction]
fn hash_tree(paths: Vec<String>) -> PyResult<Vec<(String, String, u64)>> {
    let mut files: Vec<PathBuf> = Vec::new();
    for root in paths {
        let root_path = PathBuf::from(root);
        let Ok(metadata) = fs::symlink_metadata(&root_path) else {
            continue;
        };
        if metadata.file_type().is_symlink() {
            continue;
        }
        if metadata.is_file() {
            files.push(root_path);
            continue;
        }
        if !metadata.is_dir() {
            continue;
        }
        for entry in WalkDir::new(&root_path)
            .follow_links(false)
            .into_iter()
            .filter_map(Result::ok)
        {
            if entry.file_type().is_file() && !entry.file_type().is_symlink() {
                files.push(entry.into_path());
            }
        }
    }
    files.sort();
    files.dedup();
    let mut results: Vec<(String, String, u64)> = files.par_iter().filter_map(|path| hash_path(path)).collect();
    results.sort_by(|left, right| left.0.cmp(&right.0));
    Ok(results)
}

#[pymodule]
fn styler_rust(_py: Python, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(hash_file, module)?)?;
    module.add_function(wrap_pyfunction!(hash_tree, module)?)?;
    module.add("HASH_ALGORITHM", "blake2b-128")?;
    Ok(())
}
