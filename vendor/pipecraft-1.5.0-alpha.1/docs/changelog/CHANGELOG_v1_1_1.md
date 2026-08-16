# PipeCraft v1.1.1 documentation iteration

Version: `1.1.1-alpha.1`

This iteration does not intentionally change runtime behavior. It adjusts the
project positioning and documentation so PipeCraft is described as an adaptive,
domain-agnostic pipeline runtime rather than a tool centered on specific cases
such as CI/CD, scraping, Selenium, or n8n-like workflows.

## Documentation changes

- Rewrote the README around the core product promise:

  > If a problem can be expressed as steps, dependencies, inputs, outputs,
  > policies, artifacts, and reports, PipeCraft should help make it executable,
  > repeatable, observable, and easier to evolve.

- Clarified that examples are **recipes**, not product boundaries.
- Added `docs/concepts.md` explaining the core primitives:
  - pipeline;
  - step;
  - dependency;
  - executor;
  - policy;
  - artifact;
  - report;
  - plugin.
- Added `docs/recipes.md` to explain how domain-specific use cases should be
  documented without making the core domain-specific.
- Rewrote `docs/v1_1-executors.md` to describe V1.1 additions as generic runtime
  capabilities.
- Updated `docs/migration-from-python.md` to reflect V1.1 per-run folders,
  timeout enforcement, plugin usage, and the agnostic core/plugin boundary.
- Updated workspace package metadata from `1.1.0-alpha.1` to `1.1.1-alpha.1`.

## Design principle added

```text
Core agnostic, plugins specific, recipes demonstrative, UI optional.
```

## Verification note

This package was documentation-adjusted in an environment without `cargo`.
Run locally:

```sh
cargo fmt
cargo test
cargo clippy --all-targets --all-features -- -D warnings
cargo build --release
```
