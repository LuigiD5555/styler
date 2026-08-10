use crate::error::EngineError;
use crate::protocol::{ExecutionEvent, JournalSummary};
use serde_json::Value;
use std::collections::BTreeSet;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};

pub struct Journal {
    path: PathBuf,
    writer: BufWriter<File>,
}

impl Journal {
    pub fn open(path: &Path) -> Result<Self, EngineError> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|error| EngineError::io(parent, error))?;
        }
        let file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(path)
            .map_err(|error| EngineError::io(path, error))?;
        Ok(Self {
            path: path.to_path_buf(),
            writer: BufWriter::new(file),
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn append(&mut self, event: &ExecutionEvent, durable: bool) -> Result<(), EngineError> {
        serde_json::to_writer(&mut self.writer, event)?;
        self.writer
            .write_all(b"\n")
            .map_err(EngineError::io_without_path)?;
        self.writer.flush().map_err(EngineError::io_without_path)?;
        if durable {
            self.writer
                .get_ref()
                .sync_data()
                .map_err(EngineError::io_without_path)?;
        }
        Ok(())
    }

    pub fn flush(&mut self) -> Result<(), EngineError> {
        self.writer.flush().map_err(EngineError::io_without_path)?;
        self.writer
            .get_ref()
            .sync_data()
            .map_err(EngineError::io_without_path)
    }
}

pub fn summarize(path: &Path) -> Result<JournalSummary, EngineError> {
    let file = File::open(path).map_err(|error| EngineError::io(path, error))?;
    let reader = BufReader::new(file);
    let mut run_id = String::new();
    let mut status = "incomplete".to_string();
    let mut events = 0usize;
    let mut last_sequence = 0u64;
    let mut completed = BTreeSet::new();
    let mut failed = BTreeSet::new();
    let mut skipped = BTreeSet::new();
    let mut cancelled = BTreeSet::new();
    let mut unsupported = BTreeSet::new();
    let mut in_flight = BTreeSet::new();
    let mut truncated_lines = 0usize;

    for line in reader.lines() {
        let line = line.map_err(EngineError::io_without_path)?;
        if line.trim().is_empty() {
            continue;
        }
        let value: Value = match serde_json::from_str(&line) {
            Ok(value) => value,
            Err(_) => {
                truncated_lines += 1;
                continue;
            }
        };
        events += 1;
        last_sequence = value
            .get("sequence")
            .and_then(Value::as_u64)
            .unwrap_or(last_sequence);
        if run_id.is_empty() {
            run_id = value
                .get("run_id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
        }
        let event = value
            .get("event")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let step_id = value
            .get("step_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        match event {
            "run_finished" => {
                status = value
                    .get("data")
                    .and_then(|data| data.get("status"))
                    .and_then(Value::as_str)
                    .unwrap_or("finished")
                    .to_string();
            }
            "run_cancelled" => status = "cancelled".to_string(),
            "step_started" => {
                if !step_id.is_empty() {
                    in_flight.insert(step_id);
                }
            }
            "step_completed" | "step_simulated" => {
                in_flight.remove(&step_id);
                completed.insert(step_id);
            }
            "step_failed" => {
                in_flight.remove(&step_id);
                failed.insert(step_id);
            }
            "step_skipped" => {
                in_flight.remove(&step_id);
                skipped.insert(step_id);
            }
            "step_cancelled" => {
                in_flight.remove(&step_id);
                cancelled.insert(step_id);
            }
            "step_unsupported" => {
                in_flight.remove(&step_id);
                unsupported.insert(step_id);
            }
            _ => {}
        }
    }

    Ok(JournalSummary {
        path: path.to_string_lossy().to_string(),
        run_id,
        status,
        events,
        last_sequence,
        completed_steps: completed.into_iter().collect(),
        failed_steps: failed.into_iter().collect(),
        skipped_steps: skipped.into_iter().collect(),
        cancelled_steps: cancelled.into_iter().collect(),
        unsupported_steps: unsupported.into_iter().collect(),
        in_flight_steps: in_flight.into_iter().collect(),
        truncated_lines,
    })
}

#[cfg(test)]
mod tests {
    use super::{summarize, Journal};
    use crate::protocol::{ExecutionEvent, ENGINE_VERSION, EVENT_PROTOCOL_VERSION};
    use serde_json::json;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn event(sequence: u64, kind: &str, step_id: &str) -> ExecutionEvent {
        ExecutionEvent {
            event_protocol_version: EVENT_PROTOCOL_VERSION,
            engine_version: ENGINE_VERSION,
            sequence,
            timestamp_ms: 0,
            run_id: "run-test".to_string(),
            event: kind.to_string(),
            step_id: step_id.to_string(),
            message: String::new(),
            data: json!({}),
        }
    }

    #[test]
    fn reconstructs_incomplete_steps() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!("styler-journal-{unique}.jsonl"));
        {
            let mut journal = Journal::open(&path).unwrap();
            journal.append(&event(1, "step_started", "a"), true).unwrap();
            journal.append(&event(2, "step_started", "b"), true).unwrap();
            journal.append(&event(3, "step_completed", "a"), true).unwrap();
        }
        let summary = summarize(&path).unwrap();
        assert_eq!(summary.completed_steps, vec!["a"]);
        assert_eq!(summary.in_flight_steps, vec!["b"]);
        fs::remove_file(path).ok();
    }
}
