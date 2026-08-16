# PipeCraft v1.1 alpha changes

Version: `1.1.0-alpha.1`

## Core/runtime

- Fixed routing label extraction so both `#ci` and `ci` work.
- Made `routes.yaml` parsing fail loudly on invalid YAML instead of silently falling back to no routes.
- Strengthened validation for:
  - empty `with.argv`;
  - non-scalar argv entries;
  - invalid `timeout`, `retries`, `retry_delay`;
  - invalid `copy_or_sync` config;
  - accidental `on_error.types.<step-id>` usage.
- Added `--from <step>` and `--only a,b` for partial reruns.
- Changed reports to per-run folders:
  - `.pipelines/runs/<run-id>/report.json`
  - `.pipelines/runs/<run-id>/logs/`
  - `.pipelines/runs/<run-id>/artifacts/`
- Added stdout/stderr log capture for process-based executors.
- Added enforced command/plugin timeouts.
- Added retries and retry delay for command/plugin execution.

## Executors

Added V1.1 built-ins:

- `boundary_check`
- `git_diff`
- `copy_or_sync`
- `plugin`

## Examples/docs

- Fixed `on_error.types.lint` to `on_error.steps.lint`.
- Added `examples/03-maintenance-scraper` for Selenium/scraper-style pipelines.
- Updated open-core example to use `boundary_check`.
- Updated JSON Schema with V1.1 executor examples.
- Rewrote `docs/v1_1-executors.md` with runtime additions and plugin protocol.

## Verification note

The ZIP was edited and YAML/JSON files were parsed successfully in this environment, but Rust compilation was not executed because `cargo` is not installed in the sandbox. Run locally:

```sh
cargo fmt
cargo test
cargo clippy --all-targets --all-features -- -D warnings
cargo build --release
```
