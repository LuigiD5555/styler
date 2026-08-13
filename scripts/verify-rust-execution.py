#!/usr/bin/env python3
"""Verifica el contrato JSONL del executor Rust."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("events")
    parser.add_argument("--expect-mode", choices=("dry_run", "apply"), default="dry_run")
    parser.add_argument("--expect-status", action="append", default=[])
    args = parser.parse_args()

    lines = [
        line.strip()
        for line in Path(args.events).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    events = [json.loads(line) for line in lines]
    if not events:
        raise SystemExit("no se emitieron eventos")
    if [event.get("sequence") for event in events] != list(range(1, len(events) + 1)):
        raise SystemExit("las secuencias de eventos no son consecutivas")
    if events[0].get("event") != "run_started":
        raise SystemExit("el primer evento no es run_started")
    if events[-1].get("event") != "run_finished":
        raise SystemExit("el último evento no es run_finished")

    summary = events[-1].get("data") or {}
    expected_dry_run = args.expect_mode == "dry_run"
    if bool(summary.get("dry_run")) != expected_dry_run:
        raise SystemExit(
            f"dry_run inesperado: {summary.get('dry_run')} para {args.expect_mode}"
        )
    if expected_dry_run and any(event.get("event") == "command_started" for event in events):
        raise SystemExit("un dry_run no debe iniciar comandos")
    allowed_statuses = set(args.expect_status)
    if not allowed_statuses:
        allowed_statuses = {"simulated", "blocked"} if expected_dry_run else {"completed"}
    if summary.get("status") not in allowed_statuses:
        raise SystemExit(f"estado inesperado: {summary.get('status')}")
    journal = Path(str(summary.get("journal_path", "")))
    if not journal.is_file():
        raise SystemExit(f"no se creó el journal: {journal}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
