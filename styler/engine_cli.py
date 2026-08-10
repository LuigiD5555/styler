"""CLI de diagnóstico y ejecución protegida del motor Rust."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from styler.engine_client import (
    EngineClient,
    EngineCommandError,
    EngineExecutionError,
    EngineProtocolError,
    EngineUnavailableError,
)


def _json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="styler-engine-bridge",
        description="Cliente Python del motor Rust de Styler.",
    )
    parser.add_argument("--binary", help="Ruta explícita a styler-engine")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Comprueba si el motor está disponible")
    sub.add_parser("host", help="Detecta el contexto del equipo")

    scan = sub.add_parser("scan", help="Escanea y calcula BLAKE2b-128")
    scan.add_argument("paths", nargs="+")

    hash_file = sub.add_parser("hash-file", help="Calcula el hash de un archivo")
    hash_file.add_argument("path")

    diagnose = sub.add_parser("diagnose", help="Valida el catálogo declarativo")
    diagnose.add_argument("catalog_root")

    plan = sub.add_parser("plan", help="Genera un plan sin ejecutarlo")
    plan.add_argument("request", help="Archivo JSON de solicitud")

    execute = sub.add_parser("execute", help="Simula o ejecuta un plan con eventos JSONL")
    execute.add_argument("request", help="Archivo JSON con plan y options")
    execute.add_argument(
        "--apply",
        action="store_true",
        help="Permite cambios reales; sin esta opción siempre se fuerza dry_run",
    )
    execute.add_argument(
        "--confirm-system-changes",
        action="store_true",
        help="Confirmación adicional obligatoria junto con --apply",
    )

    journal = sub.add_parser("journal-summary", help="Reconstruye el estado de un journal")
    journal.add_argument("path")

    registry_list = sub.add_parser("registry-list", help="Lista instalaciones registradas")
    registry_list.add_argument("--registry")
    registry_show = sub.add_parser("registry-show", help="Muestra un recibo de instalación")
    registry_show.add_argument("record_id")
    registry_show.add_argument("--registry")
    registry_audit = sub.add_parser("registry-audit", help="Verifica instalaciones administradas")
    registry_audit.add_argument("--registry")
    uninstall_plan = sub.add_parser("uninstall-plan", help="Genera plan seguro de desinstalación")
    uninstall_plan.add_argument("record_id")
    uninstall_plan.add_argument("--registry")
    reconcile = sub.add_parser("reconcile", help="Compara recibos con el sistema real")
    reconcile.add_argument("--registry")
    reconcile_show = sub.add_parser("reconcile-show", help="Reconcilia un recibo")
    reconcile_show.add_argument("record_id")
    reconcile_show.add_argument("--registry")
    repair_plan = sub.add_parser("repair-plan", help="Genera un plan revisable para reparar drift")
    repair_plan.add_argument("record_id")
    repair_plan.add_argument("--registry")
    adoption_preview = sub.add_parser("adoption-preview", help="Comprueba una adopción externa sin registrarla")
    adoption_preview.add_argument("request")
    registry_adopt = sub.add_parser("registry-adopt", help="Registra explícitamente una instalación externa")
    registry_adopt.add_argument("request")
    registry_adopt.add_argument("--registry")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = EngineClient(args.binary)
    try:
        if args.command == "status":
            status = client.status()
            _json(status.__dict__)
            return 0 if status.available else 2
        if args.command == "host":
            _json(client.host())
        elif args.command == "scan":
            _json(client.scan(args.paths))
        elif args.command == "hash-file":
            _json(client.hash_file(args.path))
        elif args.command == "diagnose":
            _json(client.diagnose(args.catalog_root))
        elif args.command == "plan":
            request = json.loads(Path(args.request).read_text(encoding="utf-8"))
            _json(client.plan(request))
        elif args.command == "execute":
            if args.apply and not args.confirm_system_changes:
                print(
                    "Error: --apply requiere también --confirm-system-changes",
                    file=sys.stderr,
                )
                return 2
            request = json.loads(Path(args.request).read_text(encoding="utf-8"))
            for event in client.stream_execute(
                request,
                allow_system_changes=bool(args.apply and args.confirm_system_changes),
            ):
                print(json.dumps(event, ensure_ascii=False), flush=True)
        elif args.command == "journal-summary":
            _json(client.journal_summary(args.path))
        elif args.command == "registry-list":
            _json(client.registry_list(args.registry))
        elif args.command == "registry-show":
            _json(client.registry_show(args.record_id, args.registry))
        elif args.command == "registry-audit":
            _json(client.registry_audit(args.registry))
        elif args.command == "uninstall-plan":
            _json(client.uninstall_plan(args.record_id, args.registry))
        elif args.command == "reconcile":
            _json(client.reconcile(args.registry))
        elif args.command == "reconcile-show":
            _json(client.reconcile_show(args.record_id, args.registry))
        elif args.command == "repair-plan":
            _json(client.repair_plan(args.record_id, args.registry))
        elif args.command == "adoption-preview":
            request = json.loads(Path(args.request).read_text(encoding="utf-8"))
            _json(client.adoption_preview(request))
        elif args.command == "registry-adopt":
            request = json.loads(Path(args.request).read_text(encoding="utf-8"))
            _json(client.registry_adopt(request, args.registry))
        return 0
    except (
        EngineUnavailableError,
        EngineProtocolError,
        EngineCommandError,
        EngineExecutionError,
        OSError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
