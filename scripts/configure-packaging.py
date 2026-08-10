#!/usr/bin/env python3
"""Replace public-release identity without hard-coding an invented project URL."""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(old, new), encoding="utf-8")


def ensure_field(path: Path, anchor: str, field: str, value: str) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    prefix = f"{field}:"
    lines = [line for line in lines if not line.startswith(prefix)]
    index = next(i for i, line in enumerate(lines) if line.startswith(anchor)) + 1
    lines.insert(index, f"{field}:        {value}" if path.suffix == ".spec" else f"{field}: {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="maintainer display name")
    parser.add_argument("--email", required=True, help="public maintainer email")
    parser.add_argument("--url", default="", help="public source repository URL")
    args = parser.parse_args()

    identity = f"{args.name} <{args.email}>"
    for relative in ("debian/control", "debian/changelog", "packaging/rpm/styler.spec"):
        replace(ROOT / relative, "Styler contributors <noreply@example.invalid>", identity)
    replace(ROOT / "packaging/arch/PKGBUILD", "# Maintainer: Styler contributors", f"# Maintainer: {identity}")

    if args.url:
        control = ROOT / "debian/control"
        text = control.read_text(encoding="utf-8")
        if "Homepage:" not in text:
            text = text.replace("Rules-Requires-Root: no\n", f"Rules-Requires-Root: no\nHomepage: {args.url}\n")
        control.write_text(text, encoding="utf-8")

        pkgbuild = ROOT / "packaging/arch/PKGBUILD"
        text = pkgbuild.read_text(encoding="utf-8")
        if not any(line.startswith("url=") for line in text.splitlines()):
            text = text.replace("license=('Apache-2.0')\n", f"license=('Apache-2.0')\nurl=\"{args.url}\"\n")
        pkgbuild.write_text(text, encoding="utf-8")

        spec = ROOT / "packaging/rpm/styler.spec"
        text = spec.read_text(encoding="utf-8")
        if not any(line.startswith("URL:") for line in text.splitlines()):
            text = text.replace("License:        Apache-2.0\n", f"License:        Apache-2.0\nURL:            {args.url}\n")
        spec.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
