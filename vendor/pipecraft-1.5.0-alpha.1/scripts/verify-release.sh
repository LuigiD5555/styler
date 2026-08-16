#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

echo '== Rust format =='
cargo fmt --all -- --check

echo '== Rust check =='
cargo check --workspace

echo '== Rust tests =='
cargo test --workspace

echo '== Rust clippy =='
cargo clippy --workspace --all-targets --all-features -- -D warnings

echo '== Rust release build =='
cargo build --release

echo '== Resident service smoke test =='
"$ROOT_DIR/scripts/smoke-service.sh"

echo '== Python thin-client tests =='
cd "$ROOT_DIR/python"
PYTHONPATH=src python -m pytest -q
python -m compileall -q src examples

echo 'PipeCraft release verification passed.'
