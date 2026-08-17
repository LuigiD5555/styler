#!/usr/bin/env bash
# Cuando se ejecuta normalmente usamos modo estricto. Cuando se carga con
# `source`, no cambiamos las opciones del shell interactivo del usuario.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -Eeuo pipefail
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/../../pyproject.toml" ]]; then
  SOURCE_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
  INSTALL_SCRIPT="$SCRIPT_DIR/install.sh"
else
  SOURCE_DIR="$SCRIPT_DIR"
  INSTALL_SCRIPT="$SOURCE_DIR/install.sh"
fi
ASSUME_YES=0

usage() {
  cat <<'USAGE'
Instalador sencillo de Styler

Uso:
  scripts/local/install-styler.sh
  scripts/local/install-styler.sh --yes
  source scripts/local/install-styler.sh [--yes]  # también actualiza PATH en esta terminal

El instalador:
  1. Detecta la distribución Linux.
  2. Instala Python 3 y soporte venv si hacen falta.
  3. Crea un entorno privado para Styler.
  4. Instala o actualiza Styler sin usar Miniconda.
  5. Crea el comando "styler" y la entrada del menú.
  6. Configura automáticamente ~/.local/bin en PATH.

--yes  Acepta automáticamente la instalación de dependencias del sistema.
USAGE
}

while (($#)); do
  case "$1" in
    --yes|-y) ASSUME_YES=1 ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Opción desconocida: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

args=(--install-dependencies)
if [[ $ASSUME_YES -eq 1 ]]; then
  args+=(--yes)
fi

# Si se invoca de la forma habitual, install.sh deja
# ~/.local/bin persistido en los archivos de inicio del shell. Un proceso hijo
# no puede modificar el PATH de su shell padre; si el usuario *sourcea* este
# wrapper, además actualizamos ese mismo shell de inmediato.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  "$INSTALL_SCRIPT" "${args[@]}"
  BIN_HOME="${XDG_BIN_HOME:-$HOME/.local/bin}"
  case ":$PATH:" in
    *":$BIN_HOME:"*) ;;
    *) export PATH="$BIN_HOME:$PATH" ;;
  esac
  hash -r 2>/dev/null || true
  unset SCRIPT_DIR SOURCE_DIR INSTALL_SCRIPT ASSUME_YES BIN_HOME
  unset -v args 2>/dev/null || true
  return 0
fi

exec "$INSTALL_SCRIPT" "${args[@]}"
