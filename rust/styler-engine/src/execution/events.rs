use crate::error::EngineError;
use crate::journal::Journal;
use crate::protocol::{ExecutionEvent, ENGINE_VERSION, EVENT_PROTOCOL_VERSION};
use serde_json::Value;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub(crate) struct EventEmitter<W: Write> {
    writer: W,
    journal: Journal,
    run_id: String,
    sequence: u64,
    journal_output: bool,
}

impl<W: Write> EventEmitter<W> {
    pub(crate) fn new(
        writer: W,
        journal: Journal,
        run_id: String,
        journal_output: bool,
    ) -> Self {
        Self {
            writer,
            journal,
            run_id,
            sequence: 0,
            journal_output,
        }
    }

    pub(crate) fn run_id(&self) -> &str {
        &self.run_id
    }

    pub(crate) fn journal_path(&self) -> String {
        self.journal.path().to_string_lossy().to_string()
    }

    pub(crate) fn journal_parent(&self) -> PathBuf {
        self.journal
            .path()
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .to_path_buf()
    }

    pub(crate) fn emit(
        &mut self,
        event: &str,
        step_id: &str,
        message: impl Into<String>,
        data: Value,
    ) -> Result<(), EngineError> {
        self.sequence += 1;
        let payload = ExecutionEvent {
            event_protocol_version: EVENT_PROTOCOL_VERSION,
            engine_version: ENGINE_VERSION.to_string(),
            sequence: self.sequence,
            timestamp_ms: now_ms(),
            run_id: self.run_id.clone(),
            event: event.to_string(),
            step_id: step_id.to_string(),
            message: message.into(),
            data,
        };
        serde_json::to_writer(&mut self.writer, &payload)?;
        self.writer
            .write_all(b"\n")
            .map_err(EngineError::io_without_path)?;
        self.writer.flush().map_err(EngineError::io_without_path)?;

        let is_output = event == "command_output" || event == "heartbeat";
        if !is_output || self.journal_output {
            self.journal.append(&payload, !is_output)?;
        }
        Ok(())
    }

    pub(crate) fn finish(&mut self) -> Result<(), EngineError> {
        self.writer.flush().map_err(EngineError::io_without_path)?;
        self.journal.flush()
    }
}

pub(crate) fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}
