#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$($ROOT/scripts/project-version.py)"
OUT="$ROOT/dist/packages/arch"
RUNTIME="${STYLER_RUNTIME:-$ROOT/dist/runtime/styler-$VERSION.pyz}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
command -v makepkg >/dev/null 2>&1 || { echo "Falta makepkg." >&2; exit 2; }
[[ ${EUID:-$(id -u)} -ne 0 ]] || { echo "makepkg no debe ejecutarse como root." >&2; exit 2; }
[[ -f "$RUNTIME" ]] || RUNTIME="$($ROOT/scripts/build-portable-runtime.sh)"
cp "$ROOT/packaging/release/arch/PKGBUILD" "$WORK/PKGBUILD"
cp "$ROOT/packaging/release/arch/styler.install" "$WORK/styler.install"
cp "$RUNTIME" "$WORK/styler.pyz"
cp "$ROOT/packaging/linux/styler.desktop" "$WORK/"
cp "$ROOT/packaging/linux/styler-package.xml" "$WORK/"
cp "$ROOT/docs/styler.1" "$ROOT/LICENSE" "$ROOT/NOTICE" "$ROOT/README.md" "$WORK/"
cp "$ROOT/docs/STYLER.md" "$WORK/STYLER.md"
(cd "$WORK" && makepkg --clean --force --noconfirm)
mkdir -p "$OUT"
find "$WORK" -maxdepth 1 -type f -name '*.pkg.tar.*' -exec cp -f {} "$OUT/" \;
for package in "$OUT"/*.pkg.tar.*; do
  [[ -f "$package" ]] || continue
  sha256sum "$package" > "$package.sha256"
done
printf 'Paquete pacman de release: %s\n' "$OUT"
