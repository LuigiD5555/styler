# PipeCraft v1.5.0-alpha.1 — resident IPC runtime and durable jobs

## Runtime service

- Added `pipecraft-service`, a Rust crate containing the versioned local IPC protocol, resident service, durable job store, and a small Rust client.
- Added `pipecraft serve` with shared Tokio worker, pipeline, task, and named-resource limits.
- Unix uses a workspace-local domain socket (`.pipelines/pipecraft.sock`) with mode `0600`; non-Unix builds use loopback TCP as the current alpha fallback.
- Added service commands: `submit`, `status`, `jobs`, `cancel`, `resume-job`, `job-events`, and `job-report`.
- Submitted jobs are independent of the client process: closing the CLI/SDK does not cancel them.

## Persistence and recovery

- Added durable job records under `.pipelines/runtime/jobs/<run_id>.json`.
- Accepted jobs snapshot their validated pipeline source under the run directory; queued/resumed work cannot silently change when the source YAML is edited later.
- Completed attempts are retained under `attempts/attempt-NNN.json` while `report.json` remains the latest view.
- Job records persist original invocation intent, lifecycle, attempt count, report/state/event locations, and recovery warnings.
- Startup first checks for an atomically completed final report; if one exists it repairs the job record. Otherwise stale `queued`/`running` records become `interrupted`.
- `--recovery manual` is the default; `auto` is explicitly opt-in.
- Resume reuses the existing scheduler `state.json` and plan fingerprint instead of inventing a second DAG recovery mechanism.
- Scheduler state, job records, and final reports use temporary writes, file sync, rename, and parent-directory sync where supported; jobs are not acknowledged unless durable intent can be persisted.
- Final reports remain under the existing run directory.

## Safety semantics

- PipeCraft does not claim universal exactly-once side effects. A node interrupted after an external mutation but before durable success may be replayed.
- Manual recovery makes that uncertainty visible. Domain executors can provide stronger safety with idempotency, receipts, verification, transactional APIs, or compensation.
- Submitted jobs execute an immutable YAML snapshot. The scheduler fingerprint still fails closed if that snapshot/state/invocation combination no longer matches.

## Thin SDK

- Python now speaks `pipecraft.ipc/v1` directly using the standard library.
- `run()` is only `submit -> wait -> report`; no scheduler or process runtime exists in Python.
- Added Python `Job` view plus `submit`, `status`, `jobs`, `cancel`, `resume`, `events`, `report`, `wait`, and `ping`.
- Static one-shot operations (`list`, `validate`, `plan`) still delegate to the Rust CLI.

## Compatibility

- `pipecraft/v1` pipeline YAML remains unchanged.
- `pipecraft run` and `pipecraft run-many` remain available for embedded/one-shot use.
- The resident service is optional.
- A workspace runtime lock prevents a one-shot runtime and the service from becoming competing execution authorities over the same workspace.
- Graceful Ctrl+C requests cancellation of active jobs before the service exits.
