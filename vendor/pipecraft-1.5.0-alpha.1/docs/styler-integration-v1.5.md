# Styler integration target — PipeCraft 1.5

PipeCraft 1.5 lets Styler stop owning the lifetime of the execution engine.

Recommended boundary:

```text
Styler Python/TUI
  ├─ changesets / target state
  ├─ package/component compilation
  ├─ receipts / backups / reconciliation
  └─ PipeCraft IPC client
             │
             ▼
      PipeCraft Rust service
        ├─ DAG scheduling
        ├─ global resources
        ├─ process supervision
        ├─ events / cancellation
        ├─ durable job lifecycle
        └─ resume of persisted nodes
```

## Integration sequence

1. Start one PipeCraft service for the Styler workspace/session (or supervise it as a user service).
2. Compile a Styler change into a PipeCraft pipeline definition.
3. Submit it and persist only the returned `run_id` in Styler UI/application state.
4. Observe progress through `status` plus paged `events`.
5. On TUI restart, reconnect by `run_id`; do not rebuild an in-memory scheduler.
6. On PipeCraft `interrupted`, let Styler decide whether domain reconciliation is required before calling `resume`.
7. After success, Styler verifies the real system and commits its receipts/current-state model.

## Ownership rule

PipeCraft owns execution durability. Styler owns semantic durability.

PipeCraft can prove which DAG nodes were durably reported successful. Styler can answer stronger questions such as whether an APT package, overlay, or configuration effect currently exists and whether it is safe to remove or repeat.

This is why PipeCraft 1.5 deliberately exposes interruption uncertainty instead of hiding it behind automatic retries.
