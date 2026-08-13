#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1]).resolve()
dist = root / "dists" / "stable"
files = sorted(
    path for path in dist.rglob("*")
    if path.is_file() and path.name not in {"Release", "InRelease", "Release.gpg"}
)

lines = [
    "Origin: Styler",
    "Label: Styler",
    "Suite: stable",
    "Codename: stable",
    f"Date: {datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S %z')}",
    "Architectures: amd64 arm64 all",
    "Components: main",
    "Description: Styler package repository",
]
for algorithm, title in (("md5", "MD5Sum"), ("sha256", "SHA256")):
    lines.append(f"{title}:")
    for path in files:
        data = path.read_bytes()
        digest = hashlib.new(algorithm, data).hexdigest()
        relative = path.relative_to(dist).as_posix()
        lines.append(f" {digest} {len(data):16d} {relative}")
(dist / "Release").write_text("\n".join(lines) + "\n", encoding="utf-8")
