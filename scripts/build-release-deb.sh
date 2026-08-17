#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$($ROOT/scripts/project-version.py)"
OUT="$ROOT/dist/packages/deb"
RUNTIME="${STYLER_RUNTIME:-$ROOT/dist/runtime/styler-$VERSION.pyz}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v dpkg-deb >/dev/null 2>&1 || {
  echo "Falta dpkg-deb." >&2
  exit 2
}
[[ -f "$RUNTIME" ]] || RUNTIME="$($ROOT/scripts/build-portable-runtime.sh)"
PKG="$WORK/styler_${VERSION}-1_amd64"
mkdir -p "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/usr/lib/styler" \
  "$PKG/usr/share/applications" "$PKG/usr/share/mime/packages" \
  "$PKG/usr/share/man/man1" "$PKG/usr/share/doc/styler" \
  "$PKG/usr/share/licenses/styler" "$PKG/usr/libexec/styler"
install -m 0755 "$RUNTIME" "$PKG/usr/lib/styler/styler.pyz"
install -m 0755 "$ROOT/runtime/pipecraft/linux-x86_64/pipecraft" "$PKG/usr/libexec/styler/pipecraft"
cat > "$PKG/usr/bin/styler" <<'SH'
#!/bin/sh
exec python3 /usr/lib/styler/styler.pyz "$@"
SH
chmod 0755 "$PKG/usr/bin/styler"
install -m 0644 "$ROOT/packaging/linux/styler.desktop" "$PKG/usr/share/applications/styler.desktop"
install -m 0644 "$ROOT/packaging/linux/styler-package.xml" "$PKG/usr/share/mime/packages/styler-package.xml"
gzip -9 -c "$ROOT/docs/styler.1" > "$PKG/usr/share/man/man1/styler.1.gz"
install -m 0644 "$ROOT/README.md" "$ROOT/docs/STYLER.md" "$PKG/usr/share/doc/styler/"
install -m 0644 "$ROOT/LICENSE" "$ROOT/NOTICE" "$PKG/usr/share/licenses/styler/"
cat > "$PKG/DEBIAN/control" <<EOF2
Package: styler
Version: ${VERSION}-1
Section: utils
Priority: optional
Architecture: amd64
Maintainer: Styler contributors <noreply@example.invalid>
Depends: python3 (>= 3.10)
Recommends: kdialog | zenity
Description: integrate semantic, reproducible changes on Linux
 Styler resolves complete changes into visible pipelines with providers, backups,
 verification, progress reporting and rollback information.
 This release package includes its Python application dependencies so it also
 works on distributions whose Textual package is too old for the Styler TUI.
EOF2
cat > "$PKG/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
command -v update-mime-database >/dev/null 2>&1 && update-mime-database /usr/share/mime || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database /usr/share/applications || true
exit 0
SH
cat > "$PKG/DEBIAN/postrm" <<'SH'
#!/bin/sh
set -e
command -v update-mime-database >/dev/null 2>&1 && update-mime-database /usr/share/mime || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database /usr/share/applications || true
exit 0
SH
chmod 0755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/postrm"
mkdir -p "$OUT"
dpkg-deb --root-owner-group --build "$PKG" "$OUT/styler_${VERSION}-1_amd64.deb"
dpkg-deb --info "$OUT/styler_${VERSION}-1_amd64.deb" >/dev/null
sha256sum "$OUT/styler_${VERSION}-1_amd64.deb" > "$OUT/styler_${VERSION}-1_amd64.deb.sha256"
printf 'Paquete DEB de release: %s\n' "$OUT/styler_${VERSION}-1_amd64.deb"
