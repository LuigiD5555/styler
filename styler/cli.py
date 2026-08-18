"""CLI compacta de Styler 0.7: cambios, constructor, líneas base y .stylerpkg."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from styler.baselines import BaselineKind, BaselineService
from styler.changes import ChangeService
from styler.paths import ensure_library_root
from styler.portable import PackageType, PortableLibrary, inspect_package
from styler.ui.constructor import ChangeConstructorService


def _root(args) -> Path:
    return ensure_library_root(getattr(args, "root", None))


def _parse_change_options(raw_values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in raw_values or []:
        key, separator, value = raw.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"Opción inválida '{raw}'. Usa CLAVE=VALOR.")
        result[key.strip()] = value.strip()
    return result


def _print_change_workflow(workflow, *, show_candidates: bool = False) -> None:
    from styler.planning.graph import topological_order
    order = topological_order(workflow.steps)
    by_id = {step.id: step for step in workflow.steps}
    print(f"{str(workflow.operation).upper()} DAG · {workflow.name}")
    for position, step_id in enumerate(order, 1):
        step = by_id[step_id]
        dependencies = ", ".join(step.needs) if step.needs else "—"
        print(f"{position:>2}. {step.id}")
        print(f"    intención: {step.operation or step.step_type}")
        print(f"    método:   {step.method_id or 'sin seleccionar'}")
        if step.method_reason:
            print(f"    criterio: {step.method_reason}")
        print(f"    después de: {dependencies}")
        sequence = step.config.get("selected_semantic_sequence") or []
        for item in sequence:
            position_label = item.get("position", "?")
            label = item.get("label") or item.get("operation") or "operación"
            method_id = item.get("method_id") or "sin método"
            print(f"      {position_label}. {label} → {method_id}")
        if show_candidates:
            for candidate in step.method_candidates:
                marker = "✓" if candidate.get("method_id") == step.method_id else "·"
                state = "disponible" if candidate.get("available") else candidate.get("reason", "no disponible")
                print(f"      {marker} {candidate.get('method_id')} · {state}")


def _print_trace(path: Path, *, raw_json: bool = False) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"No se pudo leer la traza {path}: {exc}", file=sys.stderr)
        return 2
    if raw_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(f"Traza {payload.get('run_id', path.parent.name)} · {str(payload.get('operation', 'generic')).upper()} · {payload.get('workflow', '')}")
    planned = payload.get("planned_order") or []
    actual_start = payload.get("actual_start_order") or []
    actual_finish = payload.get("actual_finish_order") or []
    if planned:
        print(f"Orden del DAG:      {' → '.join(planned)}")
    if actual_start:
        print(f"Inicio real:        {' → '.join(actual_start)}")
    if actual_finish:
        print(f"Finalización real:  {' → '.join(actual_finish)}")
    for step in payload.get("nodes", []):
        print(f"{int(step.get('planned_position', step.get('position', 0))):>2}. {step.get('node_id')} [{step.get('status')}] ")
        print(f"    {step.get('semantic_operation') or step.get('step_type')} → {step.get('method_id') or 'sin método'}")
    print(f"Archivo: {path}")
    return 0


def _change(args) -> int:
    service = ChangeService(root=_root(args), home=getattr(args, "home", None))
    action = getattr(args, "change_command", getattr(args, "action", ""))
    if action == "list":
        for item in [*service.available_changes(), *service.integrated_changes()]:
            print(f"{item.change_id}\t{item.status_label}\t{item.name}")
        return 0
    if action == "trace":
        candidate = Path(args.run_id)
        path = candidate if candidate.is_file() else _root(args) / ".styler" / "runs" / args.run_id / "semantic-trace.json"
        return _print_trace(path, raw_json=bool(args.json))
    if action in {"plan", "methods"}:
        try:
            options = _parse_change_options(getattr(args, "option", None))
            plan = service.build_plan(args.change_id, args.provider, options)
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        operation = getattr(args, "operation", "both")
        workflows = []
        if operation in {"apply", "both"}:
            workflows.append(plan.workflow)
        if operation in {"remove", "both"} and plan.undo_workflow is not None:
            workflows.append(plan.undo_workflow)
        if operation == "remove" and not workflows:
            print("No existe Undo DAG: todavía no hay recibos vivos de este cambio.", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            from dataclasses import asdict
            print(json.dumps({"change_id": args.change_id, "workflows": [
                {"name": wf.name, "operation": wf.operation, "metadata": dict(wf.metadata), "steps": [asdict(step) for step in wf.steps]}
                for wf in workflows
            ]}, indent=2, ensure_ascii=False))
            return 0
        print(f"Cambio: {plan.name} · proveedor={plan.provider_label}")
        for workflow in workflows:
            _print_change_workflow(workflow, show_candidates=bool(getattr(args, "methods", False) or action == "methods"))
        return 0
    if action in {"integrate", "apply"}:
        if not args.execute:
            args.change_command = "plan"
            args.operation = "apply"
            args.methods = True
            args.json = False
            return _change(args)
        if not args.approve:
            print("La ejecución requiere --approve después de revisar el plan.", file=sys.stderr)
            return 2
        try:
            options = _parse_change_options(getattr(args, "option", None))
            result = service.execute(args.change_id, args.provider, options=options)
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(result.message)
        return 0 if result.ok else 1
    if action == "remove":
        if not service.can_rollback(args.change_id):
            print("No hay recibos vivos para construir el Undo DAG.", file=sys.stderr)
            return 1
        workflow = service.rollback_plan(args.change_id)
        if not args.execute:
            _print_change_workflow(workflow, show_candidates=True)
            return 0
        if not args.approve:
            print("La retirada requiere --approve.", file=sys.stderr)
            return 2
        result = service.rollback_change(args.change_id)
        print(result.message)
        return 0 if result.ok else 1
    return 2


def _baseline(args) -> int:
    service = BaselineService(root=_root(args))
    if args.action == "list":
        for item in service.list():
            print(f"{item.baseline_id}\t{','.join(item.labels) or item.kind_label}\t{item.name}")
        return 0
    if args.action == "capture":
        definition, problems = service.capture(
            kind=BaselineKind.OFFICIAL if args.official else BaselineKind.CUSTOM,
            baseline_id=args.baseline_id, name=args.name, clean_install=args.clean_install,
            installation_profile=args.installation_profile, activate_after=not args.no_activate,
        )
        print(definition.baseline_id)
        for problem in problems: print(f"ADVERTENCIA: {problem}", file=sys.stderr)
        if args.export:
            print(service.export_package(definition.baseline_id, args.export))
        return 0
    if args.action == "import":
        print(service.import_package(args.path, activate_after=args.activate).baseline_id); return 0
    if args.action == "export":
        print(service.export_package(args.baseline_id, args.destination)); return 0
    if args.action == "use":
        print(service.activate(args.baseline_id).baseline_id); return 0
    if args.action == "delete":
        service.remove(args.baseline_id); return 0
    if args.action == "repair":
        for item in service.repair_catalog(): print(item)
        return 0
    return 2


def _constructor(args) -> int:
    service = ChangeConstructorService(root=_root(args), home=getattr(args, "home", None))
    if args.action == "scan":
        summary = service.refresh(scope="all")
        print(f"baseline={summary.baseline_id or 'none'} inventory={summary.current_id}")
        for item in summary.detected:
            print(f"{item.change_id}\t{item.category_label}\t{item.name}\t{item.role}")
        return 0
    if args.action in {"plan", "export"}:
        if args.scan:
            service.refresh(scope="all")
        service.select(args.select)
        plan = service.generated_plan(args.package_id, args.name or args.package_id)
        print("\n".join(plan.summary))
        for item, reason in plan.skipped:
            print(f"OMITIDO\t{item}\t{reason}")
        for item in plan.warnings:
            print(f"AVISO\t{item}")
        if args.action == "plan":
            print("\n".join(plan.details))
            return 0
        result = service.build_package(
            args.destination, args.package_id, args.name or args.package_id, plan=plan,
        )
        print(result.path)
        return 1 if plan.skipped else 0
    return 2


def _package(args) -> int:
    root = _root(args)
    library = PortableLibrary(root)
    if args.action == "list":
        for package in library.list_packages():
            print(f"{package.identity}\t{package.manifest.name}")
        return 0
    if args.action == "inspect":
        inspection = inspect_package(args.path)
        print(json.dumps(inspection.manifest.to_dict(), indent=2, ensure_ascii=False)); return 0
    if args.action == "import":
        inspection = inspect_package(args.path)
        if inspection.manifest.package_type is PackageType.BASELINE:
            definition = BaselineService(root=root).import_package(args.path, activate_after=args.activate)
            print(f"baseline:{definition.baseline_id}")
        else:
            package = library.import_package(args.path, collision_policy="replace_explicitly" if args.replace else "reject")
            print(package.identity)
            print("Disponible en `styler change list`; se aplica con `styler change apply`.")
        return 0
    if args.action == "export":
        print(library.export(args.package_id, args.destination, args.version or None)); return 0
    if args.action == "delete":
        library.remove(args.package_id, args.version or None); return 0
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="styler")
    sub = parser.add_subparsers(dest="command", required=True)

    change = sub.add_parser("change", help="Planifica, integra o retira cambios del catálogo")
    cs = change.add_subparsers(dest="change_command", required=True)
    p = cs.add_parser("list")
    p.add_argument("--root", default=None)
    p.add_argument("--home", default=None)
    p.set_defaults(func=_change)

    p = cs.add_parser("plan")
    p.add_argument("change_id")
    p.add_argument("--operation", choices=["apply", "remove", "both"], default="both")
    p.add_argument("--provider", default=None)
    p.add_argument("--option", action="append", default=None, metavar="CLAVE=VALOR")
    p.add_argument("--methods", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--root", default=None)
    p.add_argument("--home", default=None)
    p.set_defaults(func=_change)

    p = cs.add_parser("methods")
    p.add_argument("change_id")
    p.add_argument("--operation", choices=["apply", "remove", "both"], default="apply")
    p.add_argument("--provider", default=None)
    p.add_argument("--option", action="append", default=None, metavar="CLAVE=VALOR")
    p.add_argument("--json", action="store_true")
    p.add_argument("--root", default=None)
    p.add_argument("--home", default=None)
    p.set_defaults(func=_change, methods=True)

    for command_name in ("integrate", "apply"):
        p = cs.add_parser(command_name)
        p.add_argument("change_id")
        p.add_argument("--provider", default=None)
        p.add_argument("--option", action="append", default=None, metavar="CLAVE=VALOR")
        p.add_argument("--execute", action="store_true")
        p.add_argument("--approve", action="store_true")
        p.add_argument("--root", default=None)
        p.add_argument("--home", default=None)
        p.set_defaults(func=_change)

    p = cs.add_parser("remove")
    p.add_argument("change_id")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--approve", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--root", default=None)
    p.add_argument("--home", default=None)
    p.set_defaults(func=_change)

    p = cs.add_parser("trace")
    p.add_argument("run_id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--root", default=None)
    p.set_defaults(func=_change)

    baseline=sub.add_parser("baseline", help="Administra líneas base empaquetadas como .stylerpkg")
    bs=baseline.add_subparsers(dest="action", required=True)
    p=bs.add_parser("list"); p.add_argument("--root", default=None); p.set_defaults(func=_baseline)
    p=bs.add_parser("capture"); p.add_argument("--id", dest="baseline_id", default=""); p.add_argument("--name", default=""); p.add_argument("--official", action="store_true"); p.add_argument("--clean-install", action="store_true"); p.add_argument("--installation-profile", default="default"); p.add_argument("--no-activate", action="store_true"); p.add_argument("--export", default=""); p.add_argument("--root", default=None); p.set_defaults(func=_baseline)
    p=bs.add_parser("import"); p.add_argument("path"); p.add_argument("--activate", action="store_true"); p.add_argument("--root", default=None); p.set_defaults(func=_baseline)
    p=bs.add_parser("export"); p.add_argument("baseline_id"); p.add_argument("destination"); p.add_argument("--root", default=None); p.set_defaults(func=_baseline)
    p=bs.add_parser("use"); p.add_argument("baseline_id"); p.add_argument("--root", default=None); p.set_defaults(func=_baseline)
    p=bs.add_parser("delete"); p.add_argument("baseline_id"); p.add_argument("--root", default=None); p.set_defaults(func=_baseline)
    p=bs.add_parser("repair"); p.add_argument("--root", default=None); p.set_defaults(func=_baseline)

    constructor=sub.add_parser("constructor", help="Detecta, compone y exporta un cambio")
    ks=constructor.add_subparsers(dest="action", required=True)
    p=ks.add_parser("scan"); p.add_argument("--root", default=None); p.add_argument("--home", default=None); p.set_defaults(func=_constructor)
    for action in ("plan", "export"):
        p=ks.add_parser(action); p.add_argument("--package-id", required=True); p.add_argument("--name", default=""); p.add_argument("--select", action="append", required=True); p.add_argument("--scan", action="store_true"); p.add_argument("--root", default=None); p.add_argument("--home", default=None)
        if action=="export": p.add_argument("destination")
        p.set_defaults(func=_constructor)

    package=sub.add_parser("package", help="Administra el único formato .stylerpkg")
    ps=package.add_subparsers(dest="action", required=True)
    p=ps.add_parser("list"); p.add_argument("--root", default=None); p.set_defaults(func=_package)
    p=ps.add_parser("inspect"); p.add_argument("path"); p.add_argument("--root", default=None); p.set_defaults(func=_package)
    p=ps.add_parser("import"); p.add_argument("path"); p.add_argument("--replace", action="store_true"); p.add_argument("--activate", action="store_true"); p.add_argument("--root", default=None); p.set_defaults(func=_package)
    p=ps.add_parser("export"); p.add_argument("package_id"); p.add_argument("destination"); p.add_argument("--version", default=""); p.add_argument("--root", default=None); p.set_defaults(func=_package)
    p=ps.add_parser("delete"); p.add_argument("package_id"); p.add_argument("--version", default=""); p.add_argument("--root", default=None); p.set_defaults(func=_package)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # CLI debe ser directa, sin traceback por defecto
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
