# Verification — PipeCraft 1.5.0-alpha.1

## Executed in the assembly environment

- Python SDK tests using a real temporary Unix-domain socket: **2 passed**.
- Python byte-compilation for `python/src`.
- TOML parsing: **8 files**.
- YAML parsing: **25 files**.
- JSON parsing: **3 schema files**; both IPC schemas also pass Draft 2020-12 schema validation.
- Rust lexical/delimiter scan: **28 `.rs` files**.
- Clean-room ZIP extraction and repeated metadata/Python checks before delivery.
- The release script includes an end-to-end resident-service smoke test for environments with a Rust toolchain.

## Architecture checks

- The resident service, scheduler, resource manager, cancellation, process supervision, state persistence, and job persistence are Rust-owned.
- Python submits versioned NDJSON IPC requests and maps responses to small models.
- The service uses one shared `RuntimeCoordinator`, so task budgets and named resources span jobs submitted at different times and by different clients.
- Job intent is stored separately from DAG state, and the validated pipeline definition is snapshotted before acknowledgement.
- Completed attempts are archived separately from the latest `report.json`.
- Job/state/report correctness files use sync + atomic replacement; the event tail is not recovery authority.
- A workspace runtime lock prevents competing service/one-shot coordinators.
- Startup exposes unclean shutdowns as `interrupted`; manual recovery is the default.
- Resume keeps the existing plan-fingerprint fail-closed behavior.
- The IPC request size is bounded and the Unix socket is created with mode `0600`.

## Important semantic limit

The runtime cannot generically guarantee exactly-once external side effects for a node that was interrupted between mutation and durable success. Resume therefore provides safe visibility plus at-least-once replay of the uncertain node. Applications that require stronger semantics must add idempotency/reconciliation at the executor/domain layer.

## Environment limitation

The assembly container does not provide `cargo`/`rustc`, so the Rust changes could not be compiled here. **Do not promote this alpha to stable without a real Rust build.** Run:

```sh
./scripts/verify-release.sh
```

Pay particular attention to `pipecraft-service`, Unix socket lifecycle, restart recovery, concurrent submit/cancel/resume, and workspace-wide Clippy.
