#!/usr/bin/env python3
"""Comprueba la frontera Styler ↔ PipeCraft sin ejecutar efectos.

Objetivo 0.11+: Styler conserva dominio/UI/adaptadores; PipeCraft es un runtime
Rust separado. La distribución puede incluir su binario compilado, pero no su source. Este guard evita que el source de PipeCraft o el scheduler Python
histórico vuelvan accidentalmente a la ruta productiva del adaptador IPC.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
errors: list[str] = []

# PipeCraft debe ser un proyecto independiente, no una copia dentro de Styler.
for path in (ROOT / "vendor", ROOT / "third_party" / "pipecraft"):
    if path.exists():
        errors.append(f"runtime externo copiado dentro de Styler: {path.relative_to(ROOT)}")

manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
if "vendor/pipecraft" in manifest or "recursive-include pipecraft" in manifest:
    errors.append("MANIFEST.in vuelve a empaquetar source de PipeCraft")

# El adaptador productivo puede reutilizar modelos/planificación semántica de
# Styler, pero nunca su scheduler/event loop histórico.
for path in (ROOT / "styler" / "pipecraft").glob("*.py"):
    text = path.read_text(encoding="utf-8")
    for forbidden in (
        "styler.runtime.scheduler",
        "from styler.runtime.events",
        "schedule(",
        "ThreadPoolExecutor",
    ):
        if forbidden in text:
            errors.append(f"{path.relative_to(ROOT)} depende de runtime Python prohibido: {forbidden}")

# Las rutas mutadoras del producto deben declarar backend PipeCraft/auto. El
# backend local es un arnés explícito de pruebas, no una decisión silenciosa.
product_files = [
    ROOT / "styler" / "changes" / "service.py",
    ROOT / "styler" / "component_catalog" / "restore_bridge.py",
    ROOT / "styler" / "snapshot.py",
]
for path in product_files:
    text = path.read_text(encoding="utf-8")
    if "WorkflowEngine().run(" in text:
        errors.append(f"{path.relative_to(ROOT)} ejecuta WorkflowEngine sin backend explícito")

if errors:
    print("Runtime boundary FAILED:", file=sys.stderr)
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    raise SystemExit(1)

print("Runtime boundary OK: PipeCraft separado, runtime Rust compilado permitido, sin scheduler Python productivo.")
