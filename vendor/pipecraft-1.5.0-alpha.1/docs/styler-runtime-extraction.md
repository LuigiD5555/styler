# Styler runtime extraction notes

PipeCraft 1.4.0-alpha.1 consolidates **generic orchestration capabilities** that
were exercised by Styler 0.9.11. Styler is a demanding consumer, not a domain
model for PipeCraft: the core remains application-agnostic.

## Migrated into PipeCraft

The following concepts are generic and now belong to the runtime:

- executable DAG scheduling with node states;
- concurrent DAG scheduling (`max_workers`) plus multi-pipeline execution;
- dependency conditions: `all_success`, `all_complete`, `any_failed`, `always`;
- exclusive and shared resources coordinated globally across pipelines;
- capabilities through `requires` / `provides`;
- barriers;
- durable `state.json` plus plan fingerprints for safe resume;
- append-only `events.jsonl`;
- Tokio-based live stdout/stderr draining and process-output events;
- total and inactivity timeouts;
- cancellation tokens and process-group termination on Unix;
- executor registration as the extension boundary.

## Deliberately not migrated

The following remain Styler responsibilities because they encode its domain:

- APT, Flatpak, package-manager recovery, or Linux-specific package semantics;
- `.stylerpkg` package interpretation;
- component catalogs and change definitions;
- baseline/current/target reconciliation;
- receipts, effect ownership, backups, restore rules, and uninstall semantics;
- PhotoGIMP or any application-specific knowledge;
- Styler UI/TUI concepts.

## Target integration

The intended boundary is:

```text
Styler domain/compiler
        |
        | Pipeline + registered Styler executors
        v
PipeCraft runtime
        |
        | structured results/events/state
        v
Styler verifier + receipts + reconciliation
```

Styler should progressively delete its private copies of graph scheduling,
resource coordination, process observation, cancellation, and resume only after
PipeCraft passes equivalent conformance tests.
