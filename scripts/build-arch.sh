#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$($ROOT/scripts/project-version.py)"
OUT="$ROOT/dist/packages/arch"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v makepkg >/dev/null 2>&1 || {
  echo "Falta makepkg. Ejecuta este script en Arch Linux con base-devel instalado." >&2
  exit 2
}
if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
  echo "makepkg no debe ejecutarse como root. Usa un usuario normal." >&2
  exit 2
fi

ARCHIVE="$($ROOT/scripts/make-source-archive.sh "$WORK")"
cp "$ROOT/packaging/arch/PKGBUILD" "$WORK/PKGBUILD"
cp "$ROOT/packaging/arch/styler.install" "$WORK/styler.install"
HASH="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
sed -i "s/^sha256sums=.*/sha256sums=('${HASH}')/" "$WORK/PKGBUILD"
cd "$WORK"
makepkg --clean --cleanbuild --force --noconfirm
mkdir -p "$OUT"
find "$WORK" -maxdepth 1 -type f -name '*.pkg.tar.*' -exec cp -f {} "$OUT/" \;
printf 'Paquetes Arch: %s\n' "$OUT"
