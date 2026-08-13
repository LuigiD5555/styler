use crate::error::EngineError;
use flate2::read::GzDecoder;
use reqwest::blocking::Client;
use reqwest::header::{CONTENT_LENGTH, USER_AGENT};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};
use std::time::Duration;
use tar::Archive;
use tempfile::TempDir;
use zip::ZipArchive;

const DEFAULT_MAX_SIZE: u64 = 1024 * 1024 * 1024;
const COPY_BUFFER: usize = 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArtifactSpec {
    pub source: String,
    pub checksum_sha256: String,
    pub artifact_kind: String,
    pub destination: String,
    pub file_name: String,
    pub strip_components: usize,
    pub max_size_bytes: u64,
    pub desktop_entry: bool,
    pub executable_name: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtifactReceipt {
    pub source: String,
    pub staged_path: String,
    pub destination: String,
    pub checksum_sha256: String,
    pub size_bytes: u64,
    pub artifact_kind: String,
    pub rollback_path: String,
}

pub struct ArtifactWorkspace {
    temp: TempDir,
}

impl ArtifactWorkspace {
    pub fn new() -> Result<Self, EngineError> {
        Ok(Self { temp: tempfile::Builder::new().prefix("styler-artifact-").tempdir().map_err(EngineError::io_without_path)? })
    }

    pub fn acquire(&self, spec: &ArtifactSpec) -> Result<(PathBuf, u64, String), EngineError> {
        validate_spec(spec)?;
        let name = safe_file_name(spec)?;
        let partial = self.temp.path().join(format!("{name}.partial"));
        let staged = self.temp.path().join(name);
        let max_size = if spec.max_size_bytes == 0 { DEFAULT_MAX_SIZE } else { spec.max_size_bytes };
        let mut hasher = Sha256::new();
        let mut total = 0u64;
        let mut output = OpenOptions::new().create_new(true).write(true).open(&partial)
            .map_err(|e| EngineError::io(&partial, e))?;

        if spec.source.starts_with("https://") {
            let client = Client::builder().timeout(Duration::from_secs(1800)).redirect(reqwest::redirect::Policy::limited(5)).build()
                .map_err(|e| EngineError::InvalidRequest(format!("no se pudo crear cliente HTTPS: {e}")))?;
            let mut response = client.get(&spec.source).header(USER_AGENT, "Styler/0.3 artifact-acquirer").send()
                .map_err(|e| EngineError::InvalidRequest(format!("falló la descarga HTTPS: {e}")))?;
            if !response.status().is_success() {
                return Err(EngineError::InvalidRequest(format!("la descarga respondió HTTP {}", response.status())));
            }
            if let Some(value) = response.headers().get(CONTENT_LENGTH).and_then(|v| v.to_str().ok()).and_then(|v| v.parse::<u64>().ok()) {
                if value > max_size { return Err(EngineError::InvalidRequest(format!("artefacto excede el límite de {max_size} bytes"))); }
            }
            copy_limited(&mut response, &mut output, &mut hasher, &mut total, max_size)?;
        } else {
            let source = local_source(&spec.source)?;
            let mut input = File::open(&source).map_err(|e| EngineError::io(&source, e))?;
            copy_limited(&mut input, &mut output, &mut hasher, &mut total, max_size)?;
        }
        output.sync_all().map_err(|e| EngineError::io(&partial, e))?;
        let checksum = hex::encode(hasher.finalize());
        if !constant_time_eq(checksum.as_bytes(), spec.checksum_sha256.to_ascii_lowercase().as_bytes()) {
            let _ = fs::remove_file(&partial);
            return Err(EngineError::InvalidRequest(format!("checksum SHA-256 inválido: esperado {}, obtenido {checksum}", spec.checksum_sha256)));
        }
        fs::rename(&partial, &staged).map_err(|e| EngineError::io(&staged, e))?;
        Ok((staged, total, checksum))
    }

    pub fn install(&self, spec: &ArtifactSpec, staged: &Path, rollback_root: &Path) -> Result<ArtifactReceipt, EngineError> {
        let destination = expand_home(&spec.destination);
        reject_dangerous_destination(&destination)?;
        let rollback = backup_existing(&destination, rollback_root)?;
        let result = match spec.artifact_kind.as_str() {
            "appimage" | "binary" => install_single_file(staged, &destination, true),
            "zip" => install_zip(staged, &destination, spec.strip_components),
            "tar" | "tar.gz" | "tgz" => install_tar(staged, &destination, spec.strip_components, spec.artifact_kind != "tar"),
            "overlay_zip" => install_overlay_zip(staged, &destination, spec.strip_components),
            "overlay_tar" => install_overlay_tar(staged, &destination, spec.strip_components),
            other => Err(EngineError::InvalidRequest(format!("tipo de artefacto no soportado: {other}"))),
        };
        if let Err(error) = result {
            restore_backup(&destination, rollback.as_deref())?;
            return Err(error);
        }
        if spec.artifact_kind == "appimage" && spec.desktop_entry {
            create_desktop_entry(spec, &destination)?;
        }
        let metadata = fs::metadata(staged).map_err(|e| EngineError::io(staged, e))?;
        Ok(ArtifactReceipt {
            source: spec.source.clone(), staged_path: staged.display().to_string(), destination: destination.display().to_string(),
            checksum_sha256: spec.checksum_sha256.to_ascii_lowercase(), size_bytes: metadata.len(), artifact_kind: spec.artifact_kind.clone(),
            rollback_path: rollback.map(|p| p.display().to_string()).unwrap_or_default(),
        })
    }
}

fn create_desktop_entry(spec: &ArtifactSpec, executable: &Path) -> Result<(), EngineError> {
    let home = std::env::var("HOME").map_err(|_| EngineError::InvalidRequest("HOME no está definido".to_string()))?;
    let apps = PathBuf::from(home).join(".local/share/applications");
    fs::create_dir_all(&apps).map_err(|e| EngineError::io(&apps,e))?;
    let stem = if spec.executable_name.is_empty() { executable.file_stem().and_then(|v|v.to_str()).unwrap_or("application") } else { &spec.executable_name };
    let safe: String = stem.chars().map(|c| if c.is_ascii_alphanumeric() || c=='-' || c=='_' { c } else { '-' }).collect();
    let path = apps.join(format!("styler-{safe}.desktop"));
    let temp = path.with_extension("desktop.new");
    let content = format!("[Desktop Entry]\nType=Application\nName={}\nExec={}\nTerminal=false\nCategories=Utility;\nX-Styler-Managed=true\n", stem, executable.display());
    fs::write(&temp, content).map_err(|e| EngineError::io(&temp,e))?;
    fs::rename(&temp,&path).map_err(|e| EngineError::io(&path,e))?;
    Ok(())
}

fn validate_spec(spec: &ArtifactSpec) -> Result<(), EngineError> {
    if !(spec.source.starts_with("https://") || spec.source.starts_with("file://") || Path::new(&spec.source).is_absolute()) {
        return Err(EngineError::InvalidRequest("source debe ser HTTPS, file:// o ruta absoluta".to_string()));
    }
    let checksum = spec.checksum_sha256.trim();
    if checksum.len() != 64 || !checksum.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(EngineError::InvalidRequest("checksum_sha256 obligatorio y debe contener 64 dígitos hexadecimales".to_string()));
    }
    if spec.destination.trim().is_empty() { return Err(EngineError::InvalidRequest("destination es obligatorio".to_string())); }
    Ok(())
}

fn safe_file_name(spec: &ArtifactSpec) -> Result<String, EngineError> {
    let candidate = if spec.file_name.is_empty() { spec.source.rsplit('/').next().unwrap_or("artifact.bin") } else { &spec.file_name };
    if candidate.is_empty() || candidate == "." || candidate == ".." || candidate.contains('/') || candidate.contains('\\') {
        return Err(EngineError::InvalidRequest("file_name inseguro".to_string()));
    }
    Ok(candidate.to_string())
}

fn local_source(source: &str) -> Result<PathBuf, EngineError> {
    let path = PathBuf::from(source.strip_prefix("file://").unwrap_or(source));
    if !path.is_absolute() || !path.is_file() { return Err(EngineError::InvalidRequest(format!("artefacto local no válido: {}", path.display()))); }
    if fs::symlink_metadata(&path).map_err(|e| EngineError::io(&path,e))?.file_type().is_symlink() {
        return Err(EngineError::InvalidRequest("no se aceptan artefactos locales mediante enlaces simbólicos".to_string()));
    }
    Ok(path)
}

fn copy_limited<R: Read, W: Write>(input: &mut R, output: &mut W, hasher: &mut Sha256, total: &mut u64, limit: u64) -> Result<(), EngineError> {
    let mut buffer = vec![0u8; COPY_BUFFER];
    loop {
        let read = input.read(&mut buffer).map_err(EngineError::io_without_path)?;
        if read == 0 { break; }
        *total = total.checked_add(read as u64).ok_or_else(|| EngineError::InvalidRequest("tamaño desbordado".to_string()))?;
        if *total > limit { return Err(EngineError::InvalidRequest(format!("artefacto excede el límite de {limit} bytes"))); }
        hasher.update(&buffer[..read]);
        output.write_all(&buffer[..read]).map_err(EngineError::io_without_path)?;
    }
    Ok(())
}

fn install_single_file(staged: &Path, destination: &Path, executable: bool) -> Result<(), EngineError> {
    if let Some(parent)=destination.parent(){ fs::create_dir_all(parent).map_err(|e| EngineError::io(parent,e))?; }
    let temp=destination.with_extension("styler-new");
    fs::copy(staged,&temp).map_err(|e| EngineError::io(&temp,e))?;
    if executable { fs::set_permissions(&temp, fs::Permissions::from_mode(0o755)).map_err(|e| EngineError::io(&temp,e))?; }
    fs::rename(&temp,destination).map_err(|e| EngineError::io(destination,e))
}

fn install_zip(staged:&Path,destination:&Path,strip:usize)->Result<(),EngineError>{
    let temp=destination.with_extension("styler-new"); if temp.exists(){fs::remove_dir_all(&temp).map_err(|e|EngineError::io(&temp,e))?;} fs::create_dir_all(&temp).map_err(|e|EngineError::io(&temp,e))?;
    extract_zip(staged,&temp,strip)?; fs::rename(&temp,destination).map_err(|e|EngineError::io(destination,e))
}
fn install_tar(staged:&Path,destination:&Path,strip:usize,gzip:bool)->Result<(),EngineError>{
    let temp=destination.with_extension("styler-new"); if temp.exists(){fs::remove_dir_all(&temp).map_err(|e|EngineError::io(&temp,e))?;} fs::create_dir_all(&temp).map_err(|e|EngineError::io(&temp,e))?;
    extract_tar(staged,&temp,strip,gzip)?; fs::rename(&temp,destination).map_err(|e|EngineError::io(destination,e))
}
fn install_overlay_zip(staged:&Path,destination:&Path,strip:usize)->Result<(),EngineError>{ fs::create_dir_all(destination).map_err(|e|EngineError::io(destination,e))?; extract_zip(staged,destination,strip) }
fn install_overlay_tar(staged:&Path,destination:&Path,strip:usize)->Result<(),EngineError>{ fs::create_dir_all(destination).map_err(|e|EngineError::io(destination,e))?; extract_tar(staged,destination,strip,true) }

fn extract_zip(source:&Path,destination:&Path,strip:usize)->Result<(),EngineError>{
    let file=File::open(source).map_err(|e|EngineError::io(source,e))?; let mut archive=ZipArchive::new(file).map_err(|e|EngineError::InvalidRequest(format!("ZIP inválido: {e}")))?;
    let mut expanded=0u64;
    for index in 0..archive.len(){ let mut entry=archive.by_index(index).map_err(|e|EngineError::InvalidRequest(format!("ZIP inválido: {e}")))?; expanded=expanded.saturating_add(entry.size()); if expanded>4*DEFAULT_MAX_SIZE{return Err(EngineError::InvalidRequest("ZIP excede límite expandido".to_string()));}
        if entry.unix_mode().map(|m| m & 0o170000 == 0o120000).unwrap_or(false){return Err(EngineError::InvalidRequest("ZIP contiene enlace simbólico".to_string()));}
        let Some(relative)=strip_path(entry.enclosed_name().ok_or_else(||EngineError::InvalidRequest("ZIP contiene ruta insegura".to_string()))?,strip) else {continue}; let output=destination.join(relative); ensure_beneath(destination,&output)?;
        if entry.is_dir(){fs::create_dir_all(&output).map_err(|e|EngineError::io(&output,e))?;} else {if let Some(parent)=output.parent(){fs::create_dir_all(parent).map_err(|e|EngineError::io(parent,e))?;} let mut out=OpenOptions::new().create_new(true).write(true).open(&output).map_err(|e|EngineError::io(&output,e))?; io::copy(&mut entry,&mut out).map_err(EngineError::io_without_path)?;}
    } Ok(())
}
fn extract_tar(source:&Path,destination:&Path,strip:usize,gzip:bool)->Result<(),EngineError>{
    let file=File::open(source).map_err(|e|EngineError::io(source,e))?; let reader:Box<dyn Read>=if gzip{Box::new(GzDecoder::new(file))}else{Box::new(file)}; let mut archive=Archive::new(reader); let entries=archive.entries().map_err(|e|EngineError::InvalidRequest(format!("TAR inválido: {e}")))?;
    for item in entries{let mut entry=item.map_err(|e|EngineError::InvalidRequest(format!("TAR inválido: {e}")))?; let kind=entry.header().entry_type(); if kind.is_symlink()||kind.is_hard_link()||kind.is_block_special()||kind.is_char_special()||kind.is_fifo(){return Err(EngineError::InvalidRequest("TAR contiene entrada especial no permitida".to_string()));} let path=entry.path().map_err(|e|EngineError::InvalidRequest(format!("ruta TAR inválida: {e}")))?; let Some(relative)=strip_path(&path,strip)else{continue}; let output=destination.join(relative); ensure_beneath(destination,&output)?; if kind.is_dir(){fs::create_dir_all(&output).map_err(|e|EngineError::io(&output,e))?;}else if kind.is_file(){if let Some(parent)=output.parent(){fs::create_dir_all(parent).map_err(|e|EngineError::io(parent,e))?;} let mut out=OpenOptions::new().create_new(true).write(true).open(&output).map_err(|e|EngineError::io(&output,e))?; io::copy(&mut entry,&mut out).map_err(EngineError::io_without_path)?;}}
    Ok(())
}
fn strip_path(path:&Path,count:usize)->Option<PathBuf>{let components:Vec<_>=path.components().filter_map(|c|match c{Component::Normal(v)=>Some(v),_=>None}).collect(); if components.len()<=count{return None;} let mut out=PathBuf::new(); for c in &components[count..]{out.push(c);} Some(out)}
fn ensure_beneath(root:&Path,path:&Path)->Result<(),EngineError>{if !path.starts_with(root){Err(EngineError::InvalidRequest("ruta de extracción escapa del staging".to_string()))}else{Ok(())}}
fn expand_home(value:&str)->PathBuf{if let Some(rest)=value.strip_prefix("~/"){if let Ok(home)=std::env::var("HOME"){return PathBuf::from(home).join(rest);}} PathBuf::from(value)}
fn reject_dangerous_destination(path:&Path)->Result<(),EngineError>{let text=path.to_string_lossy(); if !path.is_absolute()||matches!(text.as_ref(),"/"|"/usr"|"/etc"|"/home"|"/var"){return Err(EngineError::InvalidRequest(format!("destino peligroso o demasiado amplio: {text}")));} Ok(())}
fn backup_existing(destination:&Path,rollback_root:&Path)->Result<Option<PathBuf>,EngineError>{if !destination.exists(){return Ok(None);} fs::create_dir_all(rollback_root).map_err(|e|EngineError::io(rollback_root,e))?; let backup=rollback_root.join(format!("{}-previous",destination.file_name().and_then(|v|v.to_str()).unwrap_or("artifact"))); if backup.exists(){return Err(EngineError::InvalidRequest(format!("rollback ya existe: {}",backup.display())));} fs::rename(destination,&backup).map_err(|e|EngineError::io(destination,e))?; Ok(Some(backup))}
fn restore_backup(destination:&Path,backup:Option<&Path>)->Result<(),EngineError>{if destination.exists(){if destination.is_dir(){fs::remove_dir_all(destination).map_err(|e|EngineError::io(destination,e))?;}else{fs::remove_file(destination).map_err(|e|EngineError::io(destination,e))?;}} if let Some(backup)=backup{fs::rename(backup,destination).map_err(|e|EngineError::io(destination,e))?;} Ok(())}
fn constant_time_eq(a:&[u8],b:&[u8])->bool{if a.len()!=b.len(){return false;} let mut diff=0u8; for (x,y) in a.iter().zip(b){diff|=x^y;} diff==0}

#[cfg(test)] mod tests{use super::*; #[test] fn rejects_missing_checksum(){let s=ArtifactSpec{source:"https://example.invalid/a".into(),checksum_sha256:"bad".into(),artifact_kind:"appimage".into(),destination:"/tmp/a".into(),file_name:"a".into(),strip_components:0,max_size_bytes:0,desktop_entry:false,executable_name:String::new()}; assert!(validate_spec(&s).is_err());} #[test] fn strips_components(){assert_eq!(strip_path(Path::new("root/bin/app"),1),Some(PathBuf::from("bin/app")));}}
