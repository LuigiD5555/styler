# Styler integration target for PipeCraft 1.4

PipeCraft 1.4 makes Rust the execution authority. Styler should use it as a
runtime rather than carrying another scheduler/process engine in Python.

## Candidate code to retire from Styler after conformance testing

- DAG scheduling and dependency-state transitions;
- resource acquisition/release;
- `run_if`, barriers and capability gating;
- process streaming, heartbeat, timeout and cancellation;
- runtime `events.jsonl` and resumable `state.json` mechanics;
- generic executor registry mechanics.

## Code that stays in Styler

- change/component compilation into a PipeCraft pipeline;
- APT, Flatpak and other domain executors/policies;
- `.stylerpkg` interpretation and trust rules;
- receipts, effect ownership, backups and undo semantics;
- baseline/current/target reconciliation and drift detection;
- UI/TUI and user-facing change semantics.

## Preferred boundary

```text
Styler Python/TUI/domain
        |
        | JSON CLI/IPC today; thinner native service boundary later
        v
PipeCraft Rust runtime
        |
        | events + reports + state
        v
Styler receipts/verifier/reconciliation
```

Migration should be behavior-preserving: Styler's current runtime tests should
become conformance tests and only then should duplicated Python/Rust execution
code be deleted.
