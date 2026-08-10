#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$($ROOT/scripts/project-version.py)"
OUT="$ROOT/dist/packages/deb"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for command in dpkg-buildpackage; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "Falta $command. Instala devscripts, debhelper, dh-python y pybuild-plugin-pyproject." >&2
    exit 2
  }
done

ARCHIVE="$($ROOT/scripts/make-source-archive.sh "$WORK")"
cp "$ARCHIVE" "$WORK/styler_${VERSION}.orig.tar.gz"
tar -xzf "$ARCHIVE" -C "$WORK"
cd "$WORK/styler-$VERSION"
dpkg-buildpackage --build=binary --no-sign
mkdir -p "$OUT"
find "$WORK" -maxdepth 1 -type f \( -name '*.deb' -o -name '*.buildinfo' -o -name '*.changes' \) -exec cp -f {} "$OUT/" \;
printf 'Paquetes DEB: %s\n' "$OUT"
