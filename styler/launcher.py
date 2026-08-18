"""Punto de entrada público de Styler."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from tempfile import mkdtemp

from styler import __version__
from styler.compat import detect_environment
from styler.dialogs import native_dialog_available
from styler.paths import ensure_library_root
from styler.startup import drop_sudo_root_to_invoking_user


def _run_tui(root: str, demo: bool, open_path: str, home: str | None, ascii_symbols: bool) -> int:
    try:
        from styler.tui.app import StylerApp
    except ImportError:
        print("La interfaz necesita Textual. Usa: styler cli --help", file=sys.stderr); return 2
    StylerApp(root=root, home=home, demo=demo, open_path=open_path, unicode_symbols=not ascii_symbols).run()
    return 0


def _doctor(root: str) -> int:
    environment = detect_environment()
    print("Diagnóstico de Styler")
    print(f"  Versión: {__version__}")
    print(f"  Biblioteca: {root}")
    print(f"  Escritorio: {environment.desktop}")
    print(f"  Sesión: {environment.session}")
    print(f"  Selector gráfico: {'disponible' if native_dialog_available() else 'no disponible'}")
    try:
        from styler.pipecraft.service import diagnose
        info = diagnose(Path(root))
        print(f"  PipeCraft requerido: >= {info['required_version']} ({info['required_protocol']})")
        print(f"  PipeCraft binario: {info['binary'] if info['binary_available'] else 'no encontrado'}")
        if info['service_active']:
            state = 'compatible' if info['compatible'] else 'INCOMPATIBLE'
            print(f"  PipeCraft service: activo ({info['service_version']}, {info['service_protocol']}) [{state}]")
        else:
            print("  PipeCraft service: detenido (se inicia bajo demanda cuando existe el binario)")
        if info.get('message') and info['service_active'] and not info['compatible']:
            print(f"  PipeCraft detalle: {info['message']}")
    except Exception as exc:
        print(f"  PipeCraft: diagnóstico no disponible ({exc})")
    return 0


def main(argv: list[str] | None = None) -> int:
    drop_sudo_root_to_invoking_user()
    argv = list(sys.argv[1:] if argv is None else argv)
    # Private process boundary used by PipeCraft plugins. Keeping this behind
    # the normal Styler executable makes wheel and portable .pyz distributions
    # behave identically without relying on PYTHONPATH.
    if argv and argv[0] == "__pipecraft_plugin_host":
        from styler.execution.plugin_host import main as plugin_main
        return plugin_main()
    if argv and argv[0] in {"change", "baseline", "constructor", "package"}:
        from styler.cli import main as cli_main
        return cli_main(argv)
    if argv and argv[0] == "cli":
        from styler.cli import main as cli_main
        return cli_main(argv[1:])
    if argv and not argv[0].startswith("-"):
        candidate = Path(argv[0]).expanduser()
        if candidate.is_file():
            argv = ["open", str(candidate), *argv[1:]]
    parser = argparse.ArgumentParser(prog="styler")
    parser.add_argument("--version", action="version", version=f"Styler {__version__}")
    parser.add_argument("command", nargs="?", choices=["open", "doctor"])
    parser.add_argument("file", nargs="?", default="", help="archivo .stylerpkg")
    parser.add_argument("--root", default=None)
    parser.add_argument("--home", default=None)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--ascii", action="store_true")
    args = parser.parse_args(argv)
    root = str(ensure_library_root(args.root))
    if args.demo:
        root = mkdtemp(prefix="styler-demo-")
    if args.command == "doctor":
        return _doctor(root)
    open_path = ""
    if args.command == "open":
        if not args.file or Path(args.file).suffix != ".stylerpkg":
            parser.error("solo se puede abrir un archivo .stylerpkg")
        open_path = str(Path(args.file).expanduser())
    return _run_tui(root, args.demo, open_path, args.home, args.ascii)


if __name__ == "__main__":
    raise SystemExit(main())
