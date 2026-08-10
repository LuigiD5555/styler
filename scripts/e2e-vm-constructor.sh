#!/usr/bin/env bash
set -euo pipefail

if [[ ${STYLER_E2E_DISPOSABLE:-0} != 1 ]]; then
  cat >&2 <<'EOF'
Esta prueba instala y retira software y crea recursos visuales.
Ejecuta solo dentro de una VM desechable:
  sudo STYLER_E2E_DISPOSABLE=1 PACKAGE=stacer APPIMAGE_URL=https://... ./scripts/e2e-vm-constructor.sh
EOF
  exit 2
fi
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "La prueba necesita root dentro de la VM desechable." >&2
  exit 2
fi
: "${APPIMAGE_URL:?Define APPIMAGE_URL con una AppImage real para la prueba}"
PACKAGE=${PACKAGE:-stacer}
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_USER=${SUDO_USER:-$(logname 2>/dev/null || echo root)}
TEST_HOME=$(getent passwd "$TEST_USER" | cut -d: -f6)
[[ -n "$TEST_HOME" ]] || { echo "No se pudo determinar HOME" >&2; exit 2; }
WORK=$(mktemp -d -t styler-e2e-vm-XXXXXX)
LIBRARY="$WORK/library"
OUTPUT="$WORK/constructor-e2e.stylerpkg"

cleanup() {
  apt-get remove -y "$PACKAGE" >/dev/null 2>&1 || true
  rm -rf \
    "$TEST_HOME/Applications/StylerE2E.AppImage" \
    "$TEST_HOME/.themes/StylerE2ETheme" \
    "$TEST_HOME/.local/share/icons/StylerE2EIcons" \
    "$TEST_HOME/.icons/StylerE2ECursor" \
    "$TEST_HOME/.local/share/backgrounds/styler-e2e.jpg" \
    "$TEST_HOME/.local/share/fonts/styler-e2e.ttf" \
    "$TEST_HOME/.config/gtk-3.0/styler-e2e.css"
  rm -rf "$WORK"
}
trap cleanup EXIT

PYTHONPATH="$ROOT_DIR" HOME="$TEST_HOME" python3 - <<PY
from pathlib import Path
from styler.ui.constructor import ChangeConstructorService
service = ChangeConstructorService(root=Path("$LIBRARY"), home=Path("$TEST_HOME"))
summary = service.capture_baseline(scope="all", name="Base E2E antes de los cambios")
assert summary.has_baseline
print(f"baseline={summary.baseline_id}")
PY

apt-get update -qq
apt-get install -y "$PACKAGE"
install -d -o "$TEST_USER" -g "$TEST_USER" \
  "$TEST_HOME/Applications" \
  "$TEST_HOME/.themes/StylerE2ETheme" \
  "$TEST_HOME/.local/share/icons/StylerE2EIcons" \
  "$TEST_HOME/.icons/StylerE2ECursor/cursors" \
  "$TEST_HOME/.local/share/backgrounds" \
  "$TEST_HOME/.local/share/fonts" \
  "$TEST_HOME/.config/gtk-3.0"
curl -fL "$APPIMAGE_URL" -o "$TEST_HOME/Applications/StylerE2E.AppImage"
chmod 0755 "$TEST_HOME/Applications/StylerE2E.AppImage"
printf '%s\n' 'window { color: #123456; }' > "$TEST_HOME/.themes/StylerE2ETheme/gtk.css"
printf '%s\n' '[Icon Theme]' > "$TEST_HOME/.local/share/icons/StylerE2EIcons/index.theme"
printf '%s\n' '[Icon Theme]' > "$TEST_HOME/.icons/StylerE2ECursor/index.theme"
printf 'cursor' > "$TEST_HOME/.icons/StylerE2ECursor/cursors/left_ptr"
printf 'jpeg' > "$TEST_HOME/.local/share/backgrounds/styler-e2e.jpg"
printf 'font' > "$TEST_HOME/.local/share/fonts/styler-e2e.ttf"
printf '%s\n' 'button { border-radius: 9px; }' > "$TEST_HOME/.config/gtk-3.0/styler-e2e.css"
chown -R "$TEST_USER:$TEST_USER" "$TEST_HOME/Applications" "$TEST_HOME/.themes" "$TEST_HOME/.icons" "$TEST_HOME/.local" "$TEST_HOME/.config/gtk-3.0"

PYTHONPATH="$ROOT_DIR" HOME="$TEST_HOME" python3 - <<PY
from pathlib import Path
from styler.portable import PackageType, inspect_package
from styler.ui.constructor import ChangeConstructorService
service = ChangeConstructorService(root=Path("$LIBRARY"), home=Path("$TEST_HOME"))
summary = service.refresh(scope="all")
ids = {item.change_id for item in summary.detected}
assert any(item.manager == "apt" and item.name.lower() == "$PACKAGE".lower() for item in summary.detected), ids
assert any(item.manager == "appimage" for item in summary.detected), ids
required = {"theme", "icon-theme", "cursor-theme", "wallpaper", "font", "css"}
observed = {item.category for item in summary.detected}
assert required <= observed, (required - observed, observed)
service.select_all_exportable()
result = service.build_package(Path("$OUTPUT"), package_id="constructor-e2e", name="Constructor E2E")
inspection = inspect_package(result.path)
assert inspection.manifest.package_type is PackageType.CHANGE
assert Path(result.path).suffix == ".stylerpkg"
print(f"detected={len(summary.detected)} package={result.path}")
PY

echo "E2E VM: aplicación de repositorio + AppImage + visuales + .stylerpkg: OK"
