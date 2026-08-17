#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$($ROOT/scripts/project-version.py)"
OUT="$ROOT/dist/packages/rpm"
RUNTIME="${STYLER_RUNTIME:-$ROOT/dist/runtime/styler-$VERSION.pyz}"
TOP="$(mktemp -d)"
trap 'rm -rf "$TOP"' EXIT
command -v rpmbuild >/dev/null 2>&1 || { echo "Falta rpmbuild." >&2; exit 2; }
[[ -f "$RUNTIME" ]] || RUNTIME="$($ROOT/scripts/build-portable-runtime.sh)"
mkdir -p "$TOP"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
cp "$RUNTIME" "$TOP/SOURCES/styler.pyz"
cp "$ROOT/runtime/pipecraft/linux-x86_64/pipecraft" "$TOP/SOURCES/pipecraft"
chmod 0755 "$TOP/SOURCES/pipecraft"
cp "$ROOT/packaging/linux/styler.desktop" "$TOP/SOURCES/"
cp "$ROOT/packaging/linux/styler-package.xml" "$TOP/SOURCES/"
cp "$ROOT/docs/styler.1" "$ROOT/LICENSE" "$ROOT/NOTICE" "$ROOT/README.md" "$TOP/SOURCES/"
cp "$ROOT/docs/STYLER.md" "$TOP/SOURCES/STYLER.md"
cp "$ROOT/packaging/release/rpm/styler-portable.spec" "$TOP/SPECS/"
rpmbuild --define "_topdir $TOP" -bb "$TOP/SPECS/styler-portable.spec"
mkdir -p "$OUT"
find "$TOP/RPMS" -type f -name '*.rpm' -exec cp -f {} "$OUT/" \;
for package in "$OUT"/*.rpm; do
  [[ -f "$package" ]] || continue
  sha256sum "$package" > "$package.sha256"
done
printf 'Paquete RPM de release: %s\n' "$OUT"
