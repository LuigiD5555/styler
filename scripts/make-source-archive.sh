#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$($ROOT/scripts/project-version.py)"
OUT_DIR="${1:-$ROOT/dist/source}"
NAME="styler-$VERSION"
ARCHIVE="$OUT_DIR/$NAME.tar.gz"

mkdir -p "$OUT_DIR"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/$NAME"

tar \
  --exclude='.git' \
  --exclude='.github/workflows/*.local.yml' \
  --exclude='.venv' \
  --exclude='build' \
  --exclude='dist' \
  --exclude='portable-packages' \
  --exclude='*.egg-info' \
  --exclude='__pycache__' \
  --exclude='*.py[cod]' \
  --exclude='*.log' \
  -C "$ROOT" -cf - . | tar -C "$TMP/$NAME" -xf -

# Reproducible ordering and timestamps when GNU tar is available.
if tar --help 2>/dev/null | grep -q -- '--sort'; then
  tar --sort=name --mtime='UTC 2026-07-12' --owner=0 --group=0 --numeric-owner \
    -C "$TMP" -czf "$ARCHIVE" "$NAME"
else
  tar -C "$TMP" -czf "$ARCHIVE" "$NAME"
fi
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"
printf '%s\n' "$ARCHIVE"
