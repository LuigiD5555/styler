#!/usr/bin/env python3
"""Convierte el sobre de ``styler-engine plan`` en una solicitud execute."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan_envelope")
    parser.add_argument("output")
    parser.add_argument("--mode", choices=("dry_run", "apply"), default="dry_run")
    parser.add_argument("--journal-path", default="")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    envelope = json.loads(Path(args.plan_envelope).read_text(encoding="utf-8"))
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        raise SystemExit("el archivo no contiene un plan exitoso")
    plan = envelope.get("result")
    if not isinstance(plan, dict):
        raise SystemExit("el sobre no contiene result como objeto")
    request = {
        "plan": plan,
        "options": {
            "mode": args.mode,
            "default_timeout_seconds": args.timeout,
            "continue_on_optional_failure": True,
            "elevation": "none",
            "journal_output": False,
        },
    }
    if args.journal_path:
        request["options"]["journal_path"] = args.journal_path
    Path(args.output).write_text(
        json.dumps(request, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
