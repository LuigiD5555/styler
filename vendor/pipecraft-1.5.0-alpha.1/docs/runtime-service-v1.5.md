# PipeCraft 1.5 runtime service

PipeCraft 1.5 adds a resident Rust runtime without changing the pipeline domain model. The service is an optional execution mode: the existing embedded CLI remains available for one-shot runs.

## Why a service

A client such as a TUI should not need to own the scheduler process. It can submit work, disconnect, reconnect later, inspect durable status, request cancellation, read events, and obtain the final report by `run_id`.

On Unix the default endpoint is:

```text
<workspace>/.pipelines/pipecraft.sock
```

The socket is created with mode `0600`. The protocol is newline-delimited JSON and is versioned as `pipecraft.ipc/v1`. One request is sent per connection and one response is returned.

## Start

```bash
pipecraft --root . serve \
  --max-pipelines 8 \
  --max-tasks 32 \
  --worker-threads 8
```

Safe recovery is the default:

```bash
pipecraft serve --recovery manual
```

`manual` marks jobs that were `queued` or `running` during a service restart as `interrupted`. The user or client decides whether to resume them.

`auto` immediately requeues interrupted jobs. It is opt-in because PipeCraft cannot guarantee exactly-once effects for an executor that crashed after changing an external system but before persisting success.

## Job lifecycle

```text
queued -> running -> succeeded
                  -> failed
                  -> cancelled

service restart while queued/running -> interrupted
interrupted/failed/cancelled -> resume -> queued
```

Every submitted job has a durable record:

```text
.pipelines/runtime/jobs/<run_id>.json
```

The job record stores the original execution intent and references the run-level files:

```text
.pipelines/runs/<run_id>/
├── state.json
├── events.jsonl
├── report.json
├── logs/
└── artifacts/
```

The two persistence layers have different responsibilities:

- `runtime/jobs/<run_id>.json`: service/job lifecycle and original request.
- `runs/<run_id>/state.json`: DAG node state used by the scheduler to resume confirmed successful nodes.

## IPC requests

Examples are shown as one-line JSON for clarity.

```json
{"op":"ping"}
{"op":"submit","pipeline":"build","execute":true,"max_workers":4}
{"op":"status","run_id":"..."}
{"op":"jobs"}
{"op":"cancel","run_id":"..."}
{"op":"resume","run_id":"..."}
{"op":"events","run_id":"...","after":0,"limit":200}
{"op":"report","run_id":"..."}
```

Responses use one envelope:

```json
{"protocol":"pipecraft.ipc/v1","ok":true,"data":{}}
```

or:

```json
{"protocol":"pipecraft.ipc/v1","ok":false,"error":{"code":"JOB_NOT_FOUND","message":"..."}}
```

## Resume safety model

PipeCraft persists scheduler state before execution begins and after node transitions. On resume it reuses only nodes whose successful terminal state was durably recorded and whose plan fingerprint and invocation still match.

An interrupted node is **not** assumed successful. It may be replayed. Therefore PipeCraft provides durable at-least-once recovery for the interrupted node, not universal exactly-once semantics for arbitrary external side effects. Domain executors that require stronger guarantees should use idempotency, verification, receipts, transactional APIs, or compensating actions.

This keeps PipeCraft domain agnostic while allowing applications such as Styler to add stronger semantic reconciliation above the runtime.

## Multi-pipeline scheduling

The resident service shares a single Rust `RuntimeCoordinator` across jobs. Consequently the global task budget and named resource table span all submitted pipelines, not just one batch invocation.

```text
client A -> pipeline A --\
client B -> pipeline B ----> shared coordinator -> Rust/Tokio
client C -> pipeline C --/
```

A resource such as `database:migrations`, `gpu`, or `dpkg` remains an opaque name to PipeCraft. Exclusive resources serialize across separate jobs while unrelated branches continue.

## Client ownership

The service owns execution. Clients own only intent and presentation. Closing a Python process or TUI does not cancel the submitted job. Explicit `cancel` is required.

## Immutable submitted definition

`submit` snapshots the validated pipeline source before the job is acknowledged:

```text
.pipelines/runs/<run_id>/pipeline.snapshot.yaml
```

Queued jobs and later resume attempts execute that snapshot, not whichever YAML happens to exist under `.pipelines/pipelines/` later. This prevents a Git checkout or edit from silently changing already-submitted work.

Each completed attempt is also preserved:

```text
.pipelines/runs/<run_id>/attempts/attempt-001.json
.pipelines/runs/<run_id>/attempts/attempt-002.json
...
```

`report.json` remains the latest report while the job record keeps the attempt report paths.

## Single execution authority

A workspace runtime lock under `.pipelines/runtime/service.lock` prevents a resident service and an independent one-shot Rust runtime from executing the same workspace concurrently. On Unix this is an advisory `flock`, so a stale file after a crash does not permanently block the workspace.

When `pipecraft serve` owns the workspace, use `submit`; `run` and `run-many` fail instead of creating a second resource coordinator.

## Graceful shutdown

Ctrl+C stops new accepts, requests cancellation for active jobs, and gives the process runtime a bounded shutdown window to terminate supervised process groups. A hard crash or SIGKILL is still handled on the next startup as an `interrupted` job and is never silently called successful.
