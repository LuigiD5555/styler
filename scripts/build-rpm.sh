#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$($ROOT/scripts/project-version.py)"
OUT="$ROOT/dist/packages/rpm"
TOP="$(mktemp -d)"
trap 'rm -rf "$TOP"' EXIT

command -v rpmbuild >/dev/null 2>&1 || {
  echo "Falta rpmbuild. Instala rpm-build y las dependencias Python de compilación." >&2
  exit 2
}
mkdir -p "$TOP"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
ARCHIVE="$($ROOT/scripts/make-source-archive.sh "$TOP/SOURCES")"
cp "$ROOT/packaging/rpm/styler.spec" "$TOP/SPECS/styler.spec"
rpmbuild --define "_topdir $TOP" -ba "$TOP/SPECS/styler.spec"
mkdir -p "$OUT"
find "$TOP/RPMS" "$TOP/SRPMS" -type f -name '*.rpm' -exec cp -f {} "$OUT/" \;
printf 'Paquetes RPM: %s\n' "$OUT"
