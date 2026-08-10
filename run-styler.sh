#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BIN_HOME="${XDG_BIN_HOME:-$HOME/.local/bin}"
STYLER_CMD="$BIN_HOME/styler"
SOURCE_VERSION="$(sed -nE 's/^version[[:space:]]*=[[:space:]]*"([^"]+)"/\1/p' "$SOURCE_DIR/pyproject.toml" | head -n 1)"

if [[ ! -x "$STYLER_CMD" ]]; then
  printf 'Styler todavía no está instalado. Preparando todo lo necesario…\n\n'
  "$SOURCE_DIR/install-styler.sh"
else
  INSTALLED_VERSION="$($STYLER_CMD --version 2>/dev/null | awk '{print $NF}' || true)"
  if [[ -n "$SOURCE_VERSION" && "$INSTALLED_VERSION" != "$SOURCE_VERSION" ]]; then
    # Ejecutar un ZIP nuevo debe ejecutar SU código, no conservar en silencio
    # el release anterior que ya estaba en ~/.local/bin. Solo se actualiza
    # automáticamente cuando la carpeta contiene una versión más reciente;
    # nunca se hace un downgrade implícito.
    NEWEST_VERSION="$(printf '%s\n%s\n' "$INSTALLED_VERSION" "$SOURCE_VERSION" | sort -V | tail -n 1)"
    if [[ "$NEWEST_VERSION" == "$SOURCE_VERSION" ]]; then
      printf 'Actualizando Styler %s → %s…\n\n' "${INSTALLED_VERSION:-desconocido}" "$SOURCE_VERSION"
      "$SOURCE_DIR/install-styler.sh"
    else
      printf 'Aviso: esta carpeta contiene Styler %s, pero está instalado Styler %s.\n' \
        "$SOURCE_VERSION" "${INSTALLED_VERSION:-desconocido}" >&2
      printf 'Se conservará la instalación más reciente.\n\n' >&2
    fi
  fi
fi

exec "$STYLER_CMD" "$@"
