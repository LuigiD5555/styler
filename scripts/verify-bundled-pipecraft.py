#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCH = {
    "x86_64": "linux-x86_64",
    "amd64": "linux-x86_64",
    "aarch64": "linux-aarch64",
    "arm64": "linux-aarch64",
}.get(platform.machine().lower())
if not ARCH:
    raise SystemExit(f"Unsupported architecture: {platform.machine()}")

binary = ROOT / "runtime" / "pipecraft" / ARCH / "pipecraft"
manifest_path = binary.with_name("manifest.json")
if not binary.is_file() or not os.access(binary, os.X_OK):
    raise SystemExit(f"Missing executable bundled PipeCraft: {binary}")
if not manifest_path.is_file():
    raise SystemExit(f"Missing bundled PipeCraft manifest: {manifest_path}")

manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
out = subprocess.run(
    [str(binary), "--version"], text=True, capture_output=True, check=True
).stdout.strip()
expected_version = str(manifest.get("version") or "")
if not expected_version or expected_version not in out:
    raise SystemExit(
        f"PipeCraft binary/manifest version mismatch: binary={out!r} manifest={expected_version!r}"
    )

digest = hashlib.sha256(binary.read_bytes()).hexdigest()
if manifest.get("sha256") != digest:
    raise SystemExit("Bundled PipeCraft SHA-256 does not match manifest")
print(f"OK {ARCH}: {out} sha256={digest}")
