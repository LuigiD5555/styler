# Runtime architecture — PipeCraft 1.4 alpha

## Goal

Make Rust the single execution authority while supporting high concurrency both
inside a DAG and across multiple pipelines.

## Runtime layers

```text
RuntimeManager
├─ pipeline semaphore (`max_pipelines`)
├─ global task semaphore (`max_tasks`)
├─ GlobalResourceManager
│  ├─ exclusive resources
│  └─ shared resources
└─ PipelineEngine × N
   ├─ per-pipeline `max_workers`
   ├─ DAG states / conditions / capabilities
   ├─ state.json + resume fingerprint
   ├─ events.jsonl
   └─ StepExecutor
      ├─ async command/plugin → Tokio process I/O
      └─ sync/custom → compatibility path
```

## Two levels of concurrency

1. **Inter-pipeline:** `RuntimeManager` admits up to `max_pipelines` pipelines.
2. **Intra-pipeline:** each pipeline admits up to `max_workers` ready DAG nodes.

Both consume the same global `max_tasks` budget.

## Global resources

`exclusive_resources` and `shared_resources` are opaque names. A lease is held
for the lifetime of a running node and is shared by every pipeline managed by
the same `RuntimeManager`.

This makes domain-specific constraints possible without teaching the core about
the domain. `dpkg`, `gpu`, `database:migrations`, or `local_llm` are just names.

## Async process runtime

`command`, `plugin`, and `git_diff` use `tokio::process::Command` and async
stdout/stderr readers. Output crosses a bounded channel (backpressure), is
streamed to full per-attempt log files, and only a bounded 1 MiB preview per
stream is retained in memory for reports. The runtime emits `process_output`,
heartbeat, timeout and cancellation events while the process is alive. Unix
process groups remain the cancellation boundary for descendants.

## Compatibility

- YAML stays `pipecraft/v1`.
- `PipelineEngine::run()` remains available for synchronous Rust callers.
- `PipelineEngine::run_async()` is canonical for async Rust callers.
- Existing synchronous `StepExecutor::run()` implementations remain valid;
  `run_async()` has a compatibility default.
- `--max-workers 1` still preserves conservative single-pipeline execution.

## Not included

This alpha intentionally does not add Styler concepts such as APT/Flatpak,
receipts, backups, `.stylerpkg`, baseline/current/target reconciliation, or
application catalogs.

## Multi-pipeline hardening

Global resource waits carry a monotonic resource-generation observation so a
release that races with waiter registration cannot strand a ready node. A
pipeline runtime task is nested under a supervising task so a panic produces an
explicit failed run rather than silently removing that pipeline from a batch.

Event JSONL writes use fixed sharded locks and whole-line appends to prevent
concurrent node output from interleaving JSON records without retaining one
permanent file handle per historical run.

## Release optimization

The release profile uses `opt-level = 3`, one codegen unit and thin LTO. Runtime
parallelism is independently configurable through Tokio worker threads,
`max_pipelines`, global `max_tasks`, and per-pipeline `max_workers`.
