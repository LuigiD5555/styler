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
        from styler.pipecraft.service import locate_binary, workspace_for
        from styler.pipecraft.client import PipeCraftClient
        binary = locate_binary()
        print(f"  PipeCraft 1.5: {'disponible' if binary else 'no compilado/no encontrado'}")
        if binary:
            print(f"  PipeCraft binario: {binary}")
        try:
            info = PipeCraftClient(workspace_for(Path(root))).ping()
            print(f"  PipeCraft service: activo ({info.get('version', '?')}, {info.get('protocol', '?')})")
        except Exception:
            print("  PipeCraft service: detenido (se inicia bajo demanda)")
    except Exception as exc:
        print(f"  PipeCraft: diagnóstico no disponible ({exc})")
    return 0


def main(argv: list[str] | None = None) -> int:
    drop_sudo_root_to_invoking_user()
    argv = list(sys.argv[1:] if argv is None else argv)
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
