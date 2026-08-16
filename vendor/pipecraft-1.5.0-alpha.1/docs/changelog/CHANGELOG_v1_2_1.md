# PipeCraft v1.2.1-alpha.1 — Stabilization

This release is a stabilization iteration for the Rust CLI/runtime and the Python bridge boundary. It intentionally avoids larger roadmap items such as daemons, UI, PyO3/maturin, plugin registries, multipipeline flow orchestration, schedulers, bundled wheels, or automatic binary downloads.

## Added

- Rust CLI `--json` output for:
  - `pipecraft list --json`
  - `pipecraft validate <pipeline> --json`
  - `pipecraft plan <pipeline> --json`
  - `pipecraft route <labels> --json`
  - `pipecraft run <pipeline> --json`
  - `pipecraft run-labels <labels> --json`
- Python bridge JSON-first parsing for `list`, `validate`, `plan`, `route`, `run`, and `run_labels`.
- Compatibility fallback to the older human-output parser when a legacy binary does not support `--json`.
- Python CLI parity for `run-labels`:
  - `python -m pipecraft run-labels ci`
  - `python -m pipecraft run-labels ci --execute`
  - `python -m pipecraft run-labels ci --execute --approve`
  - `python -m pipecraft run-labels ci --from parse`
  - `python -m pipecraft run-labels ci --only parse,validate`
- Real end-to-end Python ↔ Rust integration test that runs only when `target/release/pipecraft` or `target/release/pipecraft.exe` exists.
- Windows-aware binary lookup for `pipecraft.exe` in addition to `pipecraft`.
- Python plugin SDK helpers:
  - `artifacts_path()`
  - `logs_path()`
  - `artifact_path(name)`
  - `log_path(name)`
  - `ensure_artifacts_dir()`
  - `ensure_logs_dir()`
  - `write_artifact(name, content)`
  - `write_json_artifact(name, data)`
- Structured `doctor()` fields for Python version, package version, platform, binary path/version, workspace files, route file, pipeline directory, default pipeline, and JSON CLI capability.
- `python -m unittest` discovery support through `python/tests/__init__.py`.

## Changed

- Starter `hello.yaml` now uses a Python command instead of shell `echo`:

  ```yaml
  argv: ["python", "-c", "print('Hello from PipeCraft Python bridge')"]
  ```

  This is more portable across Windows, Linux, and macOS because `echo` is often a shell builtin on Windows.

- Python package version updated to `1.2.1a1`.
- Rust workspace version updated to `1.2.1-alpha.1`.
- Public repository metadata now points to `https://github.com/LuigiD5555/pipecraft`.

## Verified in this artifact

- `cd python && python -m pytest -q`
- `cd python && python -m unittest`

The integration test is skipped unless a release Rust binary is present.

## Not verified in this artifact

This environment did not include `cargo`, so Rust verification commands could not be executed here. Run locally before tagging or publishing:

```sh
cargo fmt
cargo test
cargo clippy --all-targets --all-features -- -D warnings
cargo build --release
```

## Intended acceptance checks

```sh
cargo fmt
cargo test
cargo clippy --all-targets --all-features -- -D warnings
cargo build --release

cd python
python -m pip install -e .
python -m pytest
python -m unittest

python -m pipecraft init --root /tmp/pipecraft-demo
python -m pipecraft doctor --root /tmp/pipecraft-demo
python -m pipecraft run hello --root /tmp/pipecraft-demo

../target/release/pipecraft --root /tmp/pipecraft-demo list --json
../target/release/pipecraft --root /tmp/pipecraft-demo validate hello --json
../target/release/pipecraft --root /tmp/pipecraft-demo plan hello --json
../target/release/pipecraft --root /tmp/pipecraft-demo run hello --json
```
