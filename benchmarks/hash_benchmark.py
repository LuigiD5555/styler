#!/usr/bin/env python3
"""Benchmark reproducible del backend de hashing de Styler.

Perfiles solicitados por la migración 0.13:
- 10 000 archivos pequeños;
- 1 000 archivos medianos;
- 100 archivos grandes;
- aproximadamente 1 GiB y 5 GiB totales.

La primera pasada se etiqueta ``first`` y la segunda ``warm``. Una caché de SO
verdaderamente fría requiere controles privilegiados y deliberadamente NO se
simula ni se fuerza desde este script.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
import time
from pathlib import Path
from typing import Callable

from styler.hashing import _hash_tree_python

try:
    import styler_rust  # type: ignore
except ImportError:
    styler_rust = None

MIB = 1024 * 1024
GIB = 1024 * MIB

PROFILES: dict[str, tuple[int, int]] = {
    "small-files": (10_000, 4 * 1024),
    "medium-files": (1_000, 1 * MIB),        # ~1 GiB
    "large-files": (100, 50 * MIB),          # ~5 GiB
    "mixed-1gb": (1_024, 1 * MIB),
    "mixed-5gb": (1_024, 5 * MIB),
    "smoke": (32, 8 * 1024),
}


def _write_dataset(root: Path, count: int, size: int) -> int:
    root.mkdir(parents=True, exist_ok=True)
    # Reusar un bloque determinista evita que generación aleatoria domine la prueba.
    rng = random.Random(1301)
    block = bytes(rng.randrange(0, 256) for _ in range(min(size, MIB)))
    total = 0
    for index in range(count):
        path = root / f"file-{index:06d}.bin"
        remaining = size
        with path.open("wb") as fh:
            while remaining:
                chunk = block[: min(len(block), remaining)]
                fh.write(chunk)
                remaining -= len(chunk)
        total += size
    return total


def _python(root: Path) -> int:
    return len(_hash_tree_python([str(root)]))


def _rust(root: Path) -> int:
    if styler_rust is None:
        raise RuntimeError("styler_rust no está instalado")
    return len(styler_rust.hash_tree([str(root)]))


def _measure(label: str, fn: Callable[[Path], int], root: Path) -> dict[str, object]:
    started = time.perf_counter()
    count = fn(root)
    elapsed = time.perf_counter() - started
    return {"backend": label, "files": count, "seconds": elapsed}


def run(profile: str, directory: Path | None = None) -> dict[str, object]:
    count, size = PROFILES[profile]
    context = tempfile.TemporaryDirectory(prefix="styler-hash-bench-") if directory is None else None
    root = Path(context.name) if context is not None else directory
    assert root is not None
    try:
        total = _write_dataset(root, count, size)
        passes: list[dict[str, object]] = []
        for cache_state in ("first", "warm"):
            result = _measure("python", _python, root)
            result["cache_state"] = cache_state
            passes.append(result)
            if styler_rust is not None:
                result = _measure("rust-extension", _rust, root)
                result["cache_state"] = cache_state
                passes.append(result)
        return {
            "profile": profile,
            "files": count,
            "bytes": total,
            "gib": total / GIB,
            "rust_available": styler_rust is not None,
            "cache_note": "first/warm; no privileged OS cache drop performed",
            "passes": passes,
        }
    finally:
        if context is not None:
            context.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(PROFILES), default="smoke")
    parser.add_argument("--directory", type=Path, help="Directorio vacío para conservar/reusar el dataset")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.directory is not None:
        args.directory.mkdir(parents=True, exist_ok=True)
        if any(args.directory.iterdir()):
            parser.error("--directory debe estar vacío")
    report = run(args.profile, args.directory)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Profile: {report['profile']} ({report['files']} files, {report['gib']:.3f} GiB)")
        for item in report["passes"]:
            print(f"{item['backend']:>14} {item['cache_state']:>5}: {item['seconds']:.4f}s")
        if not report["rust_available"]:
            print("Rust extension unavailable; only Python was measured.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
