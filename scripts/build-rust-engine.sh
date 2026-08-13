#!/usr/bin/env bash
set -euo pipefail

show_help() {
  cat <<'HELP'
Uso:
  ./scripts/build-rust-engine.sh [opciones]

Compila el motor Rust híbrido. No reemplaza la TUI Python. La ejecución real
está protegida y el modo predeterminado siempre es dry_run.

Opciones:
  --debug             Compila sin --release.
  --install-user      Copia el binario a ~/.local/bin/styler-engine.
  --output DIR        Copia el binario terminado dentro de DIR.
  -h, --help          Muestra esta ayuda.
HELP
}

profile="release"
install_user=0
output_dir=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --debug)
      profile="debug"
      shift
      ;;
    --install-user)
      install_user=1
      shift
      ;;
    --output)
      [ "$#" -ge 2 ] || { echo "Falta el directorio para --output" >&2; exit 2; }
      output_dir="$2"
      shift 2
      ;;
    -h|--help)
      show_help
      exit 0
      ;;
    *)
      echo "Opción desconocida: $1" >&2
      show_help >&2
      exit 2
      ;;
  esac
done

command -v cargo >/dev/null 2>&1 || {
  echo "No se encontró cargo. Instala Rust con el método recomendado para tu distribución." >&2
  exit 3
}

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
crate_dir="$project_root/rust/styler-engine"

if [ "$profile" = "release" ]; then
  cargo build --manifest-path "$crate_dir/Cargo.toml" --release
else
  cargo build --manifest-path "$crate_dir/Cargo.toml"
fi

binary="$crate_dir/target/$profile/styler-engine"
[ -x "$binary" ] || { echo "La compilación terminó sin producir $binary" >&2; exit 4; }

if [ "$install_user" -eq 1 ]; then
  install -d "$HOME/.local/bin"
  install -m 0755 "$binary" "$HOME/.local/bin/styler-engine"
  echo "Instalado: $HOME/.local/bin/styler-engine"
fi

if [ -n "$output_dir" ]; then
  install -d "$output_dir"
  install -m 0755 "$binary" "$output_dir/styler-engine"
  echo "Copiado: $output_dir/styler-engine"
fi

echo "Motor compilado: $binary"
"$binary" version
