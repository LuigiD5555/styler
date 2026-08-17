#!/usr/bin/env bash
set -euo pipefail
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN_HOME="${XDG_BIN_HOME:-$HOME/.local/bin}"
APP_DIR="$DATA_HOME/styler-app"
BRIDGE_STATE="$APP_DIR/command-bridge.path"

managed_bridge() {
  local target="$1"
  [[ -f "$target" ]] && grep -Fq '# STYLER_MANAGED_COMMAND_BRIDGE=1' "$target" 2>/dev/null
}

remove_managed_bridge() {
  local target="$1"
  [[ -n "$target" ]] || return 0
  managed_bridge "$target" || return 0
  if [[ -w "$(dirname -- "$target")" ]]; then
    rm -f -- "$target"
  elif command -v sudo >/dev/null 2>&1; then
    sudo rm -f -- "$target"
  elif command -v pkexec >/dev/null 2>&1; then
    pkexec rm -f -- "$target"
  else
    printf 'Aviso: no se pudo retirar el puente administrado %s (faltan permisos).\n' "$target" >&2
  fi
}

if [[ -f "$BRIDGE_STATE" ]]; then
  bridge="$(cat "$BRIDGE_STATE" 2>/dev/null || true)"
  remove_managed_bridge "$bridge"
fi

rm -rf "$APP_DIR"
rm -f "$BIN_HOME/styler"
rm -f "$DATA_HOME/applications/styler.desktop"
rm -f "$DATA_HOME/mime/packages/styler-package.xml"
command -v update-mime-database >/dev/null 2>&1 && update-mime-database "$DATA_HOME/mime" || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DATA_HOME/applications" || true
printf 'Styler se desinstaló. Tu biblioteca de perfiles se conservó en %s/styler.\n' "$DATA_HOME"
