#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Esta prueba instala y retira un paquete temporal; ejecútala como root dentro de una VM o contenedor desechable." >&2
  exit 2
fi
for command in dpkg-deb apt-get python3; do
  command -v "$command" >/dev/null || { echo "Falta $command" >&2; exit 2; }
done

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORK=$(mktemp -d -t styler-constructor-apt-XXXXXX)
LIBRARY="$WORK/library"
HOME_DIR="$WORK/home"
PKGROOT="$WORK/package"
PACKAGE_NAME="styler-e2e-demo"
DEB="$WORK/${PACKAGE_NAME}_1.0_all.deb"
APPLICATIONS_DIR="/usr/local/share/applications"

cleanup() {
  apt-get remove -y "$PACKAGE_NAME" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
mkdir -p "$HOME_DIR" "$PKGROOT/DEBIAN" "$PKGROOT/usr/bin" "$PKGROOT$APPLICATIONS_DIR"

cat > "$PKGROOT/DEBIAN/control" <<EOF
Package: $PACKAGE_NAME
Version: 1.0
Section: utils
Priority: optional
Architecture: all
Maintainer: Styler E2E <styler@example.invalid>
Description: Aplicación temporal para comprobar detección APT real
EOF
cat > "$PKGROOT/usr/bin/$PACKAGE_NAME" <<'EOF'
#!/bin/sh
printf '%s\n' 'Styler E2E demo'
EOF
chmod 0755 "$PKGROOT/usr/bin/$PACKAGE_NAME"
cat > "$PKGROOT$APPLICATIONS_DIR/$PACKAGE_NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Styler E2E Demo
Exec=$PACKAGE_NAME
Terminal=false
Categories=Utility;
EOF

dpkg-deb --build "$PKGROOT" "$DEB" >/dev/null

PYTHONPATH="$ROOT_DIR" HOME="$HOME_DIR" python3 - <<PY
from pathlib import Path
from styler.baselines import BaselineKind
from styler.provenance.detectors.apt import AptDetector
from styler.provenance.inventory import detect_system_identity
from styler.provenance.models import Inventory
from styler.ui.constructor import ChangeConstructorService
apps = AptDetector(applications_dirs=[Path("$APPLICATIONS_DIR")]).detect(scope="apps")
identity = detect_system_identity()
inventory = Inventory(inventory_id="apt-before", distro=identity.distro_id, system=identity, scope="apps", managers_seen=["apt"], applications=apps)
service = ChangeConstructorService(root=Path("$LIBRARY"), home=Path("$HOME_DIR"))
service.baselines.register_inventory(inventory, kind=BaselineKind.CUSTOM, baseline_id="apt-before", name="Base antes de la instalación", activate_after=True)
print("baseline=apt-before")
PY

apt-get install -y "$DEB" >/dev/null

PYTHONPATH="$ROOT_DIR" HOME="$HOME_DIR" python3 - <<PY
from pathlib import Path
from styler.provenance.detectors.apt import AptDetector
from styler.provenance.inventory import detect_system_identity, save_inventory
from styler.provenance.models import Inventory
from styler.ui.constructor import ChangeConstructorService
apps = AptDetector(applications_dirs=[Path("$APPLICATIONS_DIR")]).detect(scope="apps")
identity = detect_system_identity()
inventory = Inventory(inventory_id="apt-after", distro=identity.distro_id, system=identity, scope="apps", managers_seen=["apt"], applications=apps)
save_inventory(inventory, root=Path("$LIBRARY"))
service = ChangeConstructorService(root=Path("$LIBRARY"), home=Path("$HOME_DIR"))
summary = service.summary()
match = next((item for item in summary.detected if item.change_id == "apt:$PACKAGE_NAME"), None)
assert match is not None, [item.change_id for item in summary.detected]
assert match.role == "añadido"
assert match.category_label == "Aplicación"
# Es un .deb local sin repositorio; detectarlo no autoriza a fingir que otro equipo puede reinstalarlo.
assert match.exportable is False
print(f"detected={match.change_id} role={match.role} exportable={match.exportable}")
PY

echo "E2E APT real: OK"
