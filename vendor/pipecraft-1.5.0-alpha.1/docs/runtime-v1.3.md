# PipeCraft 1.3 runtime semantics

## DAG execution

PipeCraft still computes a stable topological order for validation, reporting,
and deterministic tie-breaking. The scheduler no longer executes that order as
a simple list. Instead each selected node transitions through runtime state.

```text
pending -> ready -> running -> terminal
```

A node becomes `ready` only after its selected dependencies are terminal and its
`run_if` condition is satisfied.

### Conditions

```yaml
run_if: all_success   # default
run_if: all_complete
run_if: any_failed
run_if: always
```

`always` still waits for declared dependencies to finish; it means "run
regardless of their result", not "ignore the DAG".

## Parallelism

The CLI defaults to one worker for compatibility:

```sh
pipecraft run demo --execute
```

Opt in to parallel branches explicitly:

```sh
pipecraft run demo --execute --max-workers 4
```

Stable topological order is used to choose among equally-ready nodes.

## Resources

Resources are opaque names supplied by pipeline authors.

```yaml
- id: migrate
  type: command
  exclusive_resources: [database]

- id: read_metrics
  type: command
  shared_resources: [database]
```

An exclusive holder conflicts with both exclusive and shared users of the same
name. Shared holders may overlap with other shared holders.

This mechanism is deliberately unaware of domains. A consumer may use names
such as `database`, `gpu`, `package-manager`, `workspace`, or anything else.

## Capabilities

Capabilities express readiness not captured by a direct edge:

```yaml
- id: prepare
  type: plugin
  provides: [dataset.ready]

- id: evaluate
  type: plugin
  requires: [dataset.ready]
```

`requires` / `provides` are opaque strings. PipeCraft only coordinates them.

## Barriers

```yaml
barrier: true
```

A barrier waits for currently running nodes and runs alone. No other node starts
while a barrier is running.

## Resume

Use an explicit run id:

```sh
pipecraft run demo --execute --run-id demo-2026-08-16
pipecraft run demo --execute --run-id demo-2026-08-16 --resume
```

Successful nodes are reused only when the saved state matches:

- plan fingerprint;
- selected node set/order;
- dry-run vs execute mode;
- labels.

Transient or failed nodes are reconsidered.

## Events

`events.jsonl` is append-only and suitable for CLI/TUI consumers to tail.
Examples include:

```text
pipeline_started
pipeline_scheduled
node_started
process_output
process_timeout
node_finished
node_blocked
node_skipped
pipeline_finished
```

## Timeouts

A command may use both limits:

```yaml
with:
  argv: ["long-running-tool"]
  timeout: 3600
  inactivity_timeout: 120
```

`timeout` limits total wall-clock runtime. `inactivity_timeout` is reset whenever
stdout/stderr activity is observed.

## Process heartbeat

While a child process is alive, PipeCraft emits periodic `process_heartbeat` events. Heartbeats are observability only: they do **not** reset `inactivity_timeout`; only actual stdout/stderr activity does.
