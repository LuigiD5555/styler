# PipeCraft v1.4.0-alpha.1 — Rust-first multi-pipeline runtime

## Architectural changes

- Tokio multi-thread runtime is now the canonical execution substrate.
- Added `RuntimeManager`, `RuntimeLimits`, `PipelineRequest`, and
  `RuntimeCoordinator`.
- Added a global task semaphore and global named-resource manager shared across
  concurrent pipelines.
- Added `pipecraft run-many` with limits for worker threads, pipelines, tasks,
  and per-pipeline DAG workers.
- Converted `command` and `plugin` runtime paths to Tokio process I/O.
- Preserved synchronous `StepExecutor::run()` source compatibility while adding
  an async scheduling path.
- `PipelineRun` reports now record `runtime: rust-tokio` and
  `global_max_tasks`.

## Python reduction

The Python runtime/SDK was reduced from roughly 1,300 source lines to 257 lines
in four files. Removed Python-side installer/scaffolding, plugin framework,
human-text CLI fallbacks, duplicated report helpers, and the Python CLI. The
remaining package is a stdlib-only JSON client plus report models.

Plugins remain supported through the language-neutral stdin/stdout JSON
protocol and no longer need the Python SDK helper.

## Compatibility

The `pipecraft/v1` YAML schema is unchanged. Single-pipeline CLI behaviour is
retained. The Python package is intentionally breaking at the convenience API
level because 1.4 removes features that belonged outside the thin client.

## Alpha hardening

- Global resource wait generation prevents missed-release stalls under races.
- Multi-pipeline supervisor turns an internal pipeline-task panic into a failed
  report instead of silently dropping the run from the result set.
- `git_diff` now follows the Tokio process path during normal async execution.
- JSONL event writes are sharded and serialized per shard to avoid concurrent
  byte interleaving.
- Release profile uses one codegen unit plus thin LTO for optimized binaries.
- Added a two-pipeline example demonstrating a resource exclusive across runs.

- Process output now uses a bounded Tokio channel, streams full logs to disk,
  and caps in-memory report capture to 1 MiB per stream to keep many verbose
  concurrent processes from scaling memory linearly with total output.
