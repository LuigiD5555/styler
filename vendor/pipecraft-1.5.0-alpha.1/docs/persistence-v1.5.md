# Persistence and recovery contract (1.5)

PipeCraft 1.5 treats persistence as part of correctness rather than optional telemetry.

## Durable layers

1. **Job record** — original submission, lifecycle, attempts, report/state/event locations.
2. **Scheduler state** — plan fingerprint, selected nodes, terminal node results.
3. **Event journal** — append-only runtime observations.
4. **Final report** — immutable summary written when an attempt completes.

If run storage cannot be prepared, executors are not started. If the service cannot persist a submitted job record, the job is not accepted.

## Restart

On service startup, records left in `queued` or `running` are converted to `interrupted` before any recovery is attempted. This makes an unclean shutdown visible instead of pretending that in-memory state survived.

With manual recovery (default), no interrupted job is automatically replayed. With auto recovery, the service resubmits using the same `run_id` and `resume=true`.

## Plan drift

The existing scheduler fingerprint remains authoritative. If the pipeline definition, selection, labels, dry-run mode, or other fingerprinted plan inputs do not match the saved state, resume fails closed with `RESUME_STATE_MISMATCH`.

## Exactly-once boundary

PipeCraft can know that a node's success was durably persisted. It cannot generically prove whether an external side effect occurred between a process action and a crash. The runtime therefore never advertises universal exactly-once execution. Applications needing it must provide domain-specific idempotency or reconciliation.

## Submitted-definition snapshot

The service stores the exact validated YAML source for each accepted job. Resume uses this snapshot so source-tree edits do not alter the meaning of an existing `run_id`.

## Attempt history

Every completed execution/resume attempt receives an immutable `attempts/attempt-NNN.json` copy before the durable job record points at it. `report.json` is the latest convenience view, not the only historical evidence.

## Durability mechanics

Scheduler state, job records, and final reports are written through a temporary file, flushed/synced, and renamed into place. Parent directories are synced where the platform allows it. `events.jsonl` is observability rather than recovery authority; losing only its last buffered tail must not cause a node to be treated as successful.
