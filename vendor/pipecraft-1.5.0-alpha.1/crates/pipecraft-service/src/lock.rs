use std::fs::{File, OpenOptions};
use std::io::{Seek, SeekFrom, Write};
use std::path::Path;

/// Workspace-level service ownership guard. On Unix this uses `flock`, so a
/// stale lock file after a crash does not block the next service instance.
#[derive(Debug)]
pub struct WorkspaceRuntimeLock {
    _file: File,
}

impl WorkspaceRuntimeLock {
    pub fn acquire(runtime_dir: &Path) -> Result<Self, String> {
        std::fs::create_dir_all(runtime_dir).map_err(|error| error.to_string())?;
        let path = runtime_dir.join("service.lock");
        let mut file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&path)
            .map_err(|error| error.to_string())?;

        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
            if rc != 0 {
                return Err(format!("another PipeCraft runtime service owns {}", runtime_dir.display()));
            }
        }

        #[cfg(not(unix))]
        {
            // The alpha non-Unix transport is loopback TCP. The endpoint bind is
            // the primary singleton guard there; keep a diagnostic lock file.
        }

        file.set_len(0).map_err(|error| error.to_string())?;
        file.seek(SeekFrom::Start(0)).map_err(|error| error.to_string())?;
        let metadata = serde_json::json!({
            "protocol": "pipecraft.service-lock/v1",
            "pid": std::process::id(),
            "started_at": chrono::Utc::now().to_rfc3339(),
        });
        file.write_all(serde_json::to_string_pretty(&metadata).unwrap_or_else(|_| "{}".into()).as_bytes())
            .map_err(|error| error.to_string())?;
        file.sync_all().map_err(|error| error.to_string())?;
        Ok(Self { _file: file })
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn second_workspace_runtime_lock_is_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let _first = WorkspaceRuntimeLock::acquire(dir.path()).unwrap();
        assert!(WorkspaceRuntimeLock::acquire(dir.path()).is_err());
    }
}
