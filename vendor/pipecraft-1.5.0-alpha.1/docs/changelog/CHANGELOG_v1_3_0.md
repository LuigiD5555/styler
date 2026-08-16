# PipeCraft v1.3.0-alpha.1 — Executable DAG runtime

Version: `1.3.0-alpha.1`

This release turns PipeCraft's DAG from an ordering aid into an execution model,
while preserving the existing `pipecraft/v1` YAML contract and keeping the core
domain-agnostic.

## Runtime

- Concurrent DAG scheduler (`--max-workers N`).
- Backward-compatible default: `--max-workers 1` preserves V1 file-order
  behavior for pipelines that never declared dependencies.
- Node lifecycle: `pending`, `ready`, `running`, successful terminal states,
  `blocked`, `skipped`, `failed`, `timeout`, and `cancelled`.
- Dependency conditions through `run_if`:
  - `all_success` (default)
  - `all_complete`
  - `any_failed`
  - `always`
- Failed descendants are blocked while independent branches may continue when
  the error policy permits it.
- Named resources:
  - `exclusive_resources`
  - `shared_resources`
- Global step barriers with `barrier: true`.
- Opaque capability contracts with `requires` / `provides`.

## Durable runs and resume

Every prepared run now has:

```text
<runs>/<run-id>/
  report.json
  state.json
  events.jsonl
  logs/
  artifacts/
```

- State is persisted before nodes are launched and after transitions.
- The engine fails closed if durable run storage cannot be prepared.
- `--run-id <id> --resume` reuses successful nodes only when the plan and
  invocation match.
- Resume guards include the plan fingerprint, selection, dry-run mode and labels
  so a dry-run cannot accidentally satisfy a later execute-mode run.

## Observability and processes

- Structured JSONL events for pipeline/node/process activity.
- `stdout` and `stderr` are drained while a process runs instead of only after
  process completion.
- Process output is emitted as `process_output` events while still being stored
  in the existing per-attempt logs/report data.
- `with.inactivity_timeout` terminates commands that stop producing activity,
  separately from the existing total `with.timeout`.
- On Unix, child commands are started in a process group and timeout termination
  targets the process group, reducing orphan descendants.

## Plugin protocol

Structured plugin failures are now parsed even when the plugin process exits
non-zero. PipeCraft preserves plugin `message`, `status`, and `data` while also
embedding process diagnostics under `data.process`.

## Python bridge

The Python bridge remains thin and delegates execution to Rust. `run()` and
`run_labels()` now accept:

```python
max_workers=...
resume=True
run_id="..."
```

`PipelineReport` exposes `events_path`, `state_path`, `plan_fingerprint`, and
`max_workers`.

## Compatibility

- Schema tag remains `pipecraft/v1`.
- Existing step types and V1.2.1 pipelines remain valid.
- Concurrency is opt-in from CLI/API because the default worker count is one.
- Existing `--from` / `--only` behavior remains: dependencies outside the
  selected subset are treated as already satisfied rather than replayed.

## Verification status

The Python bridge suite passes in the build environment (`40 passed, 1 skipped`).
The build environment used to assemble this source archive does not contain a
Rust toolchain, so `cargo check/test` must still be run on a machine with Rust
before tagging a production release.

## Cancellation

- Added a shared `CancellationToken` to stop new scheduling and allow active process executors to terminate promptly.
- CLI `Ctrl+C` is routed through the same cancellation primitive.
