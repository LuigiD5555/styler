//! PipeCraft 1.5 local runtime service.
//!
//! The service keeps the Rust scheduler resident, accepts small versioned NDJSON
//! IPC commands, persists job intent independently from pipeline state, and can
//! resume safely persisted successful nodes after a service restart.

pub mod client;
mod lock;
pub mod protocol;
pub mod server;
pub mod store;

pub use client::{default_endpoint, endpoint_path, ServiceClient};
pub use lock::WorkspaceRuntimeLock;
pub use protocol::{IpcError, IpcRequest, IpcResponse, PROTOCOL_VERSION};
pub use server::{RecoveryPolicy, RuntimeService, ServiceConfig};
pub use store::{JobRecord, JobRequest, JobStatus, JobStore};
