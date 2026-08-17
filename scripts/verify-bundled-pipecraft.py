#!/usr/bin/env python3
from __future__ import annotations
import hashlib, json, os, platform, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
ARCH = {"x86_64":"linux-x86_64","amd64":"linux-x86_64","aarch64":"linux-aarch64","arm64":"linux-aarch64"}.get(platform.machine().lower())
if not ARCH:
    raise SystemExit(f"Unsupported architecture: {platform.machine()}")
binary = ROOT / "runtime" / "pipecraft" / ARCH / "pipecraft"
if not binary.is_file() or not os.access(binary, os.X_OK):
    raise SystemExit(f"Missing executable bundled PipeCraft: {binary}")
out = subprocess.run([str(binary), "--version"], text=True, capture_output=True, check=True).stdout.strip()
if "1.5.0-alpha.1" not in out:
    raise SystemExit(f"Unexpected PipeCraft version: {out}")
digest = hashlib.sha256(binary.read_bytes()).hexdigest()
manifest_path = ROOT / "runtime" / "pipecraft" / ARCH / "manifest.json"
manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
if manifest.get("sha256") and manifest["sha256"] != digest:
    raise SystemExit("Bundled PipeCraft SHA-256 does not match manifest")
print(f"OK {ARCH}: {out} sha256={digest}")
