//! Structured JSONL runtime events.
//!
//! The event stream is intentionally append-only and domain agnostic. Consumers
//! (CLI/TUI/bridges) can tail `events.jsonl` while a run is active without
//! parsing human-oriented stdout.
//!
//! Event appends are protected by a small fixed set of sharded locks. That keeps
//! concurrent nodes from interleaving JSON bytes without creating one permanent
//! mutex/file handle per run (important for a long-lived multi-pipeline runtime).

use std::collections::hash_map::DefaultHasher;
use std::fs::OpenOptions;
use std::hash::{Hash, Hasher};
use std::io::Write;
use std::sync::{Mutex, OnceLock};

use chrono::Utc;
use serde_json::Value;

use crate::context::ExecutionContext;

const EVENT_LOCK_SHARDS: usize = 64;

fn event_locks() -> &'static [Mutex<()>] {
    static LOCKS: OnceLock<Vec<Mutex<()>>> = OnceLock::new();
    LOCKS
        .get_or_init(|| (0..EVENT_LOCK_SHARDS).map(|_| Mutex::new(())).collect())
        .as_slice()
}

fn lock_index(ctx: &ExecutionContext) -> usize {
    let mut hasher = DefaultHasher::new();
    ctx.events_path.hash(&mut hasher);
    (hasher.finish() as usize) % EVENT_LOCK_SHARDS
}

pub fn emit_event(
    ctx: &ExecutionContext,
    event: &str,
    step_id: Option<&str>,
    data: Value,
) -> std::io::Result<()> {
    if ctx.events_path.as_os_str().is_empty() {
        return Ok(());
    }

    let line = serde_json::json!({
        "ts": Utc::now().to_rfc3339(),
        "event": event,
        "run_id": ctx.run_id,
        "step_id": step_id,
        "data": data,
    });
    let mut bytes = serde_json::to_vec(&line)
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    bytes.push(b'\n');

    let locks = event_locks();
    let _guard = locks[lock_index(ctx)]
        .lock()
        .expect("event journal lock poisoned");
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&ctx.events_path)?;
    file.write_all(&bytes)?;
    file.flush()?;
    Ok(())
}
