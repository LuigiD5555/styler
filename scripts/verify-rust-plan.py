#!/usr/bin/env python3
"""Valida invariantes del plan PhotoGIMP emitido por styler-engine."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(f"Plan Rust inválido: {message}")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Uso: verify-rust-plan.py PLAN.json")
    envelope = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if envelope.get("protocol_version") != 1 or not envelope.get("ok"):
        fail("sobre JSON fallido o protocolo distinto de 1")
    plan = envelope.get("result") or {}
    if not plan.get("executable"):
        fail(f"el plan no es ejecutable: {plan.get('issues')}")
    if plan.get("selected_providers", {}).get("app.gimp") != "apt":
        fail("no respetó la preferencia APT para GIMP")
    if plan.get("selected_providers", {}).get("app.photogimp") != "archive":
        fail("no seleccionó el proveedor archive para PhotoGIMP")

    order = plan.get("order", [])
    required_order = [
        "install:app.gimp",
        "verify:app.gimp",
        "backup:app.photogimp",
        "install:app.photogimp",
        "verify:app.photogimp",
    ]
    try:
        positions = [order.index(step) for step in required_order]
    except ValueError as exc:
        fail(f"falta un paso obligatorio: {exc}")
    if positions != sorted(positions):
        fail(f"orden causal incorrecto: {required_order}")

    by_id = {step["id"]: step for step in plan.get("steps", [])}
    target = by_id.get("install:app.photogimp", {}).get("config", {}).get("target")
    if target != "/home/example/.config/GIMP":
        fail(f"PhotoGIMP no heredó config_root de GIMP APT: {target!r}")
    backup_target = by_id.get("backup:app.photogimp", {}).get("config", {}).get("target")
    if backup_target != target:
        fail("el respaldo y el overlay apuntan a rutas distintas")
    print("Plan Rust PhotoGIMP validado")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
