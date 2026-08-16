# PipeCraft v1.2.0 — Python bridge

Version: `1.2.0-alpha.1`

This release adds a **Python bridge** as a new layer over the existing Rust
runtime. It does not change runtime behavior; the Rust crates are unchanged
except for the workspace version bump.

## Added — `python/` package (`pipecraft-python`, import name `pipecraft`)

A local-first, standard-library-only Python wrapper over the `pipecraft` binary.

- **Client** (`pipecraft.PipeCraft`): `list`, `validate`, `plan`, `route`,
  `run`, `run_labels`. Supports `--execute`, `--approve`, `--from`, `--only`.
  `run`/`run_labels` return a parsed report; read-only commands return small
  typed results (`ValidationResult`, `PlanResult`, `RouteResult`).
- **Models** (`pipecraft.models`): `PipelineReport` and `StepResult` dataclasses
  mirroring the Rust `report.json` schema, with `from_dict` / `from_file`
  (JSON key `type` maps onto `step_type`).
- **Reports** (`pipecraft.reports`): `load_report`, `find_latest_report`.
- **Binary discovery** (`pipecraft.binary.find_pipecraft`): explicit path →
  `PIPECRAFT_BIN` → `PATH` → `target/release|debug/pipecraft`.
- **Installer/doctor** (`pipecraft.installer`): `version`, `doctor`,
  `init_workspace` (scaffolds `.pipelines/` from packaged templates).
- **Plugin SDK** (`pipecraft.plugin`): `plugin_main`, `PluginContext`,
  `PluginResult` implementing the `pipecraft.plugin/v1` stdin/stdout protocol,
  including exception-to-result handling.
- **CLI** (`python -m pipecraft`): `init`, `doctor`, plus `list` / `validate` /
  `plan` / `route` / `run` forwarded to the runtime. Does not replace the Rust
  CLI.
- **Templates**: `workspace.yaml`, `routes.yaml`, `hello.yaml` using the real
  `pipecraft/v1` schema.
- **Example**: `python/examples/word-count/` — a runnable Python plugin plus a
  pipeline that uses it.
- **Tests**: 25 unit tests (mocking `subprocess`; no Rust binary required),
  runnable via `pytest` or `unittest`.

## Docs

- New `docs/python-bridge.md` (install, usage, plugin authoring, limitations,
  roadmap toward an optional PyO3/maturin path).
- README gains a **Python bridge** section and doc links.

## Changed

- Workspace version bumped `1.1.1-alpha.1` → `1.2.0-alpha.1` (a new layer, so a
  minor bump rather than a patch).

## Not changed

- No Rust runtime behavior changes.
- No new Rust dependencies.
- No PyO3/maturin, no network requirement, no SaaS. Local-first preserved.

## Verification status

- **Python**: the bridge's own test suite (25 tests) was executed and passes;
  `python -m pipecraft init` / `doctor`, package import, template YAML parsing,
  and an end-to-end plugin stdin→stdout roundtrip were all exercised
  successfully.
- **Rust**: not compiled in the authoring environment (no `cargo`). The Rust
  sources are unchanged from v1.1.1 apart from the version string, so no new
  compile risk was introduced. Still run `cargo build` / `cargo test` locally.
- **End-to-end with the real binary**: not exercised here (no compiled
  `pipecraft`). Once you `cargo build --release`, the `PipeCraft` client and the
  `python -m pipecraft run` path can be validated against the examples.
