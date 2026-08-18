#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN_HOME="${XDG_BIN_HOME:-$HOME/.local/bin}"
ORIGINAL_PATH="${PATH:-}"
COMMAND_BRIDGE_STATE="$DATA_HOME/styler-app/command-bridge.path"
SYSTEM_BRIDGE_DIR="${STYLER_SYSTEM_BIN:-/usr/local/bin}"
# El instalador y todos sus hijos deben poder resolver inmediatamente los
# comandos de usuario instalados en ~/.local/bin. Esto no puede modificar el
# entorno del shell padre cuando install.sh se ejecuta como proceso separado,
# por eso configure_shell_path() también persiste la misma ruta en los archivos
# de inicio del usuario.
case ":$PATH:" in
  *":$BIN_HOME:"*) ;;
  *) export PATH="$BIN_HOME:$PATH" ;;
esac
APP_DIR="$DATA_HOME/styler-app"
REGISTRY_ROOT="$DATA_HOME/styler"
APPLICATIONS_DIR="$DATA_HOME/applications"
MIME_DIR="$DATA_HOME/mime/packages"
PYTHON_BIN="${STYLER_PYTHON:-python3}"

INSTALL_DEPENDENCIES=0
ASSUME_YES=0
STAGE_DIR=""
NEW_RELEASE_DIR=""
RELEASE_ACTIVATED=0
VENV_TEST_LOG=""
VENV_TEST_DIR=""

usage() {
  cat <<'USAGE'
Uso: ./install.sh [opciones]

Opciones:
  --install-dependencies  Si falta Python o el soporte para entornos virtuales,
                          instala el paquete correcto para la distribución.
  --yes, -y               Acepta la instalación de dependencias sin preguntar.
                          Solo tiene efecto junto con --install-dependencies.
  --help, -h              Muestra esta ayuda.

Sin opciones, el instalador hace una prueba previa. En una terminal interactiva
ofrece instalar Python y cualquier dependencia faltante; en automatizaciones se
detiene sin modificar la instalación y muestra el comando necesario.
USAGE
}

while (($#)); do
  case "$1" in
    --install-dependencies)
      INSTALL_DEPENDENCIES=1
      ;;
    --yes|-y)
      ASSUME_YES=1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'Opción desconocida: %s\n\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

cleanup() {
  if [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]]; then
    rm -rf -- "$STAGE_DIR"
  fi
  if [[ $RELEASE_ACTIVATED -eq 0 && -n "$NEW_RELEASE_DIR" && -d "$NEW_RELEASE_DIR" ]]; then
    rm -rf -- "$NEW_RELEASE_DIR"
  fi
  if [[ -n "$VENV_TEST_DIR" && -d "$VENV_TEST_DIR" ]]; then
    rm -rf -- "$VENV_TEST_DIR"
  elif [[ -n "$VENV_TEST_LOG" && -f "$VENV_TEST_LOG" ]]; then
    rm -f -- "$VENV_TEST_LOG"
  fi
}
trap cleanup EXIT

have_command() {
  command -v "$1" >/dev/null 2>&1
}

prepare_build_source() {
  local target="$1"

  # Nunca construimos el wheel dentro de la carpeta extraída por el usuario.
  # Además de mantenerla limpia, esto evita que metadatos/permisos heredados de
  # un ZIP anterior (build/, *.egg-info, etc.) interfieran con setuptools.
  "$PYTHON_BIN" - "$SOURCE_DIR" "$target" <<'PY_COPY'
from __future__ import annotations

import shutil
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2]).resolve()

ignored_exact = {
    ".git",
    ".pytest_cache",
    "build",
    "dist",
    "wheelhouse",
    "__pycache__",
}

def ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in ignored_exact
        or name.endswith(".egg-info")
        or name.endswith(".pyc")
        or name.endswith(".pyo")
    }

if target.exists():
    shutil.rmtree(target)

# copyfile copia el contenido sin intentar reproducir propietario, chmod, ACL o
# xattrs del archivo original. El árbol temporal pertenece siempre al usuario
# que está instalando Styler, incluso si el ZIP fue creado en otra máquina.
shutil.copytree(
    source,
    target,
    symlinks=True,
    copy_function=shutil.copyfile,
    ignore=ignore,
)
PY_COPY
}

configure_shell_path() {
  local marker_start="# >>> Styler user commands >>>"
  local marker_end="# <<< Styler user commands <<<"
  local path_line rc_file shell_name

  # Escribe una ruta fija porque los archivos de inicio se evalúan después.
  # BIN_HOME normalmente es $HOME/.local/bin, pero también respeta XDG_BIN_HOME.
  printf -v path_line 'export PATH="%s:$PATH"' "$BIN_HOME"

  add_path_to_file() {
    local target="$1"
    [[ -e "$target" && ! -f "$target" ]] && return 0

    if [[ -f "$target" ]] && grep -Fqx "$path_line" "$target"; then
      return 0
    fi
    if [[ -f "$target" ]] && grep -Fqx "$marker_start" "$target"; then
      return 0
    fi

    mkdir -p "$(dirname -- "$target")"
    {
      [[ ! -s "$target" ]] || printf '\n'
      printf '%s\n%s\n%s\n' "$marker_start" "$path_line" "$marker_end"
    } >> "$target"
  }

  # ~/.profile cubre sesiones de inicio de sesión y muchos escritorios Linux.
  add_path_to_file "$HOME/.profile"

  # La terminal interactiva puede no cargar ~/.profile. Configuramos además el
  # archivo correspondiente al shell actual sin borrar ni reemplazar ajustes.
  shell_name="$(basename -- "${SHELL:-bash}")"
  case "$shell_name" in
    bash) add_path_to_file "$HOME/.bashrc" ;;
    zsh)  add_path_to_file "$HOME/.zshrc" ;;
    ksh)  add_path_to_file "$HOME/.kshrc" ;;
    fish)
      rc_file="${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish"
      mkdir -p "$(dirname -- "$rc_file")"
      if ! grep -Fqx "fish_add_path --prepend '$BIN_HOME'" "$rc_file" 2>/dev/null; then
        {
          [[ ! -s "$rc_file" ]] || printf '\n'
          printf "%s\nfish_add_path --prepend '%s'\n%s\n" \
            "$marker_start" "$BIN_HOME" "$marker_end"
        } >> "$rc_file"
      fi
      ;;
  esac
}


path_has_dir() {
  local haystack="$1" needle="$2"
  case ":$haystack:" in
    *":$needle:"*) return 0 ;;
    *) return 1 ;;
  esac
}

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
  else
    run_as_root rm -f -- "$target"
  fi
}

write_command_bridge() {
  local target="$1" temp_bridge
  temp_bridge="$STAGE_DIR/styler-command-bridge"
  cat > "$temp_bridge" <<EOF
#!/bin/sh
# STYLER_MANAGED_COMMAND_BRIDGE=1
# Puente mínimo para que el comando 'styler' sea visible desde una ruta que
# ya estaba en PATH antes de ejecutar el instalador. La instalación real sigue
# perteneciendo al usuario y vive en XDG_BIN_HOME/~/.local/bin.
exec "$BIN_HOME/styler" "\$@"
EOF
  chmod 0755 "$temp_bridge"

  if [[ -e "$target" || -L "$target" ]]; then
    if ! managed_bridge "$target"; then
      return 2
    fi
  fi

  mkdir -p -- "$(dirname -- "$COMMAND_BRIDGE_STATE")"
  if [[ -w "$(dirname -- "$target")" ]]; then
    install -m 0755 "$temp_bridge" "$target"
  else
    run_as_root install -m 0755 "$temp_bridge" "$target"
  fi
  printf '%s\n' "$target" > "$COMMAND_BRIDGE_STATE"
}

configure_immediate_command() {
  local old_bridge="" target="" entry="" real_entry=""
  local -a candidates=()
  local -A seen=()

  # Si ~/.local/bin ya estaba en el PATH heredado del shell padre, no hace
  # falta ningún puente: el comando nuevo será visible inmediatamente.
  if path_has_dir "$ORIGINAL_PATH" "$BIN_HOME"; then
    if [[ -f "$COMMAND_BRIDGE_STATE" ]]; then
      old_bridge="$(cat "$COMMAND_BRIDGE_STATE" 2>/dev/null || true)"
      remove_managed_bridge "$old_bridge" || true
      rm -f -- "$COMMAND_BRIDGE_STATE"
    fi
    return 0
  fi

  add_candidate() {
    local directory="$1"
    [[ -n "$directory" && -d "$directory" ]] || return 0
    [[ -z "${seen[$directory]:-}" ]] || return 0
    candidates+=("$directory")
    seen["$directory"]=1
  }

  # No dependemos de Conda. Primero inspeccionamos TODO el PATH que heredamos
  # del shell padre y reutilizamos cualquier directorio de usuario seguro que
  # ya sea visible y escribible. Esto cubre, sin casos especiales, Python
  # instalado por el usuario, pyenv, venv, Conda y ~/bin.
  IFS=':' read -r -a path_entries <<< "$ORIGINAL_PATH"
  for entry in "${path_entries[@]}"; do
    [[ -n "$entry" && "$entry" == /* ]] || continue
    [[ "$entry" != "$BIN_HOME" ]] || continue
    [[ -d "$entry" && -w "$entry" ]] || continue
    real_entry="$(cd -- "$entry" 2>/dev/null && pwd -P || true)"
    [[ -n "$real_entry" ]] || continue
    [[ "$real_entry" == */bin ]] || continue

    case "$real_entry/" in
      "$HOME"/*)
        add_candidate "$real_entry"
        ;;
    esac
  done

  # Respaldo estándar para una máquina con Python del sistema y sin Conda,
  # pyenv ni venv. /usr/local/bin es precisamente el lugar convencional para
  # comandos locales administrados fuera del gestor de paquetes. Solo creamos
  # el puente si ESA ruta ya estaba en el PATH del shell padre, de modo que
  # `styler` sea resoluble en el mismo prompt al terminar el instalador.
  if path_has_dir "$ORIGINAL_PATH" "$SYSTEM_BRIDGE_DIR"; then
    add_candidate "$SYSTEM_BRIDGE_DIR"
  fi

  ((${#candidates[@]})) || {
    printf 'Aviso: Styler se instaló, pero el PATH original no contiene una ruta segura donde publicar el comando inmediatamente.\n' >&2
    printf 'Las terminales nuevas usarán %s directamente.\n' "$BIN_HOME" >&2
    return 0
  }

  for entry in "${candidates[@]}"; do
    target="$entry/styler"
    if [[ -e "$target" || -L "$target" ]]; then
      if ! managed_bridge "$target"; then
        printf 'Aviso: %s ya existe y no pertenece a Styler; probando otra ruta.\n' "$target" >&2
        continue
      fi
    fi

    if [[ -f "$COMMAND_BRIDGE_STATE" ]]; then
      old_bridge="$(cat "$COMMAND_BRIDGE_STATE" 2>/dev/null || true)"
      if [[ -n "$old_bridge" && "$old_bridge" != "$target" ]]; then
        remove_managed_bridge "$old_bridge" || true
      fi
    fi

    if write_command_bridge "$target"; then
      printf 'Comando inmediato disponible mediante: %s\n' "$target"
      return 0
    fi
  done

  printf 'Aviso: Styler se instaló, pero ninguna ruta visible aceptó el puente inmediato.\n' >&2
  return 0
}

run_as_root() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    "$@"
  elif have_command sudo; then
    sudo "$@"
  elif have_command pkexec; then
    pkexec "$@"
  else
    cat >&2 <<'MSG'
No se encontró sudo ni pkexec para solicitar permisos de administrador.
Ejecuta manualmente el comando indicado por Styler y vuelve a intentarlo.
MSG
    return 1
  fi
}

load_os_release() {
  OS_ID=""
  OS_ID_LIKE=""
  OS_NAME="Linux"
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    OS_ID="${ID:-}"
    OS_ID_LIKE="${ID_LIKE:-}"
    OS_NAME="${PRETTY_NAME:-${NAME:-Linux}}"
  fi
}

python_version_ok() {
  "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

create_venv() {
  local target="$1"

  if "$PYTHON_BIN" -m venv "$target"; then
    return 0
  fi

  rm -rf -- "$target"
  if "$PYTHON_BIN" -m virtualenv --version >/dev/null 2>&1; then
    "$PYTHON_BIN" -m virtualenv "$target"
    return $?
  fi

  return 1
}

venv_preflight() {
  local probe_root probe_venv
  if [[ -n "$VENV_TEST_DIR" && -d "$VENV_TEST_DIR" ]]; then
    rm -rf -- "$VENV_TEST_DIR"
  fi
  VENV_TEST_DIR=""
  VENV_TEST_LOG=""
  probe_root="$(mktemp -d "${TMPDIR:-/tmp}/styler-venv-check.XXXXXX")"
  VENV_TEST_DIR="$probe_root"
  probe_venv="$probe_root/venv"
  VENV_TEST_LOG="$probe_root/error.log"

  if create_venv "$probe_venv" >"$VENV_TEST_LOG" 2>&1 \
      && "$probe_venv/bin/python" -m pip --version >/dev/null 2>&1; then
    rm -rf -- "$probe_root"
    VENV_TEST_LOG=""
    VENV_TEST_DIR=""
    return 0
  fi

  return 1
}

venv_dependency_hint() {
  load_os_release
  local ids=" $OS_ID $OS_ID_LIKE "

  if [[ "$ids" == *" debian "* || "$ids" == *" ubuntu "* ]]; then
    printf '%s' 'sudo apt-get update && sudo apt-get install -y python3 python3-venv'
  elif [[ "$ids" == *" fedora "* || "$ids" == *" rhel "* || "$ids" == *" centos "* ]]; then
    printf '%s' 'sudo dnf install -y python3 python3-pip python3-virtualenv'
  elif [[ "$ids" == *" opensuse "* || "$ids" == *" suse "* ]]; then
    printf '%s' 'sudo zypper install -y python3 python3-pip python3-virtualenv'
  elif [[ "$ids" == *" arch "* ]]; then
    printf '%s' 'sudo pacman -S --needed python python-pip python-virtualenv'
  else
    printf '%s' 'instala Python 3.10 o posterior con soporte para venv/ensurepip o virtualenv'
  fi
}

install_venv_dependency() {
  load_os_release
  local ids=" $OS_ID $OS_ID_LIKE "

  printf 'Distribución detectada: %s\n' "$OS_NAME"

  if [[ "$ids" == *" debian "* || "$ids" == *" ubuntu "* ]]; then
    run_as_root apt-get update
    if ! run_as_root apt-get install -y python3-venv; then
      local py_minor
      py_minor="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
      printf 'Probando el paquete específico python%s-venv…\n' "$py_minor"
      run_as_root apt-get install -y "python${py_minor}-venv"
    fi
  elif [[ "$ids" == *" fedora "* || "$ids" == *" rhel "* || "$ids" == *" centos "* ]]; then
    run_as_root dnf install -y python3 python3-pip python3-virtualenv
  elif [[ "$ids" == *" opensuse "* || "$ids" == *" suse "* ]]; then
    run_as_root zypper install -y python3 python3-pip python3-virtualenv
  elif [[ "$ids" == *" arch "* ]]; then
    run_as_root pacman -S --needed --noconfirm python python-pip python-virtualenv
  else
    return 1
  fi
}

repair_venv_support() {
  local hint answer
  hint="$(venv_dependency_hint)"

  cat >&2 <<EOF
Styler comprobó Python antes de instalar y no pudo crear un entorno aislado.
Esto suele significar que falta venv/ensurepip o virtualenv.

Solución sugerida:
  $hint
EOF

  if [[ $INSTALL_DEPENDENCIES -eq 0 && -t 0 && -t 1 ]]; then
    printf '\n¿Deseas que Styler intente instalar esa dependencia ahora? [s/N] ' >&2
    read -r answer
    case "${answer,,}" in
      s|si|sí|y|yes)
        INSTALL_DEPENDENCIES=1
        ;;
      *)
        cat >&2 <<'EOF'
No se modificó la instalación. Después de instalar la dependencia, ejecuta de
nuevo instalador.
EOF
        return 1
        ;;
    esac
  fi

  if [[ $INSTALL_DEPENDENCIES -eq 0 ]]; then
    cat >&2 <<'EOF'
No se modificó la instalación. En una automatización puedes usar:
  ./install.sh --install-dependencies --yes
EOF
    return 1
  fi

  if [[ $ASSUME_YES -eq 0 ]]; then
    printf '\nStyler solicitará permisos de administrador. ¿Continuar? [s/N] ' >&2
    read -r answer
    case "${answer,,}" in
      s|si|sí|y|yes) ;;
      *)
        echo 'Operación cancelada. No se modificó la instalación.' >&2
        return 1
        ;;
    esac
  fi

  if ! install_venv_dependency; then
    cat >&2 <<EOF
Styler no pudo instalar automáticamente la dependencia en esta distribución.
Ejecuta manualmente:
  $hint
EOF
    return 1
  fi

  echo 'Dependencia instalada. Styler repetirá la comprobación…'
  if ! venv_preflight; then
    cat >&2 <<'EOF'
La dependencia se instaló, pero Python todavía no puede crear un entorno con
pip. Revisa el diagnóstico mostrado abajo o utiliza un paquete nativo/AppImage.
EOF
    [[ -n "$VENV_TEST_LOG" && -f "$VENV_TEST_LOG" ]] && tail -n 20 "$VENV_TEST_LOG" >&2
    return 1
  fi
}

ensure_python_runtime() {
  local hint answer
  if have_command "$PYTHON_BIN"; then
    return 0
  fi

  hint="$(venv_dependency_hint)"
  cat >&2 <<EOF
Styler no encontró $PYTHON_BIN. Para funcionar necesita Python 3.10 o posterior,
pero no requiere Miniconda ni modificar el Python global con pip.

Solución sugerida para este sistema:
  $hint
EOF

  if [[ $INSTALL_DEPENDENCIES -eq 0 && -t 0 && -t 1 ]]; then
    printf '
¿Deseas que Styler instale Python y prepare su entorno privado? [S/n] ' >&2
    read -r answer
    case "${answer,,}" in
      n|no)
        echo 'No se modificó el sistema.' >&2
        return 1
        ;;
      *) INSTALL_DEPENDENCIES=1 ;;
    esac
  fi

  if [[ $INSTALL_DEPENDENCIES -eq 0 ]]; then
    cat >&2 <<'EOF'
No se modificó el sistema. Para automatizar la preparación ejecuta:
  ./install.sh --install-dependencies --yes
EOF
    return 1
  fi

  if [[ $ASSUME_YES -eq 0 && -t 0 && -t 1 ]]; then
    printf '
Se solicitarán permisos de administrador para instalar Python. ¿Continuar? [S/n] ' >&2
    read -r answer
    case "${answer,,}" in
      n|no)
        echo 'Operación cancelada. No se modificó el sistema.' >&2
        return 1
        ;;
    esac
  fi

  install_venv_dependency || {
    cat >&2 <<EOF
Styler no pudo instalar Python automáticamente.
Ejecuta manualmente:
  $hint
EOF
    return 1
  }

  if ! have_command "$PYTHON_BIN"; then
    echo "La instalación terminó, pero $PYTHON_BIN todavía no está disponible." >&2
    return 1
  fi
}

ensure_python_runtime || exit 1

if ! python_version_ok; then
  printf 'Styler requiere Python 3.10 o posterior. Versión encontrada: ' >&2
  "$PYTHON_BIN" --version >&2 || true
  exit 1
fi

# Punto cero automático: se ejecuta antes de instalar venv u otras dependencias.
# Es de solo lectura y únicamente recorre gestores de paquetes y rutas de
# configuración incluidas en la allowlist de Styler. Si un entorno mínimo no
# puede importar todavía el proyecto, la instalación continúa y el post-scan
# creará igualmente la línea base.
mkdir -p "$REGISTRY_ROOT"
printf 'Registrando el estado previo del sistema…\n'
if ! PYTHONPATH="$SOURCE_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -m styler.system_registry install-pre \
    --root "$REGISTRY_ROOT" --home "$HOME" >/dev/null 2>&1; then
  printf 'Aviso: no se pudo completar el registro previo; se intentará al finalizar.\n' >&2
fi

printf 'Comprobando Python y la creación de entornos aislados…\n'
if ! venv_preflight; then
  repair_venv_support || exit 1
fi

PROJECT_VERSION="$($PYTHON_BIN - <<PYVER
from pathlib import Path
import re
text = Path(r"$SOURCE_DIR/pyproject.toml").read_text(encoding="utf-8")
match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
if not match:
    raise SystemExit(1)
print(match.group(1))
PYVER
)"

mkdir -p "$DATA_HOME" "$APP_DIR/versions"
STAGE_DIR="$(mktemp -d "$DATA_HOME/styler-install.XXXXXX")"
BUILD_SOURCE_DIR="$STAGE_DIR/source"
RELEASE_ID="${PROJECT_VERSION}-$(date +%Y%m%d%H%M%S)-$$"
NEW_RELEASE_DIR="$APP_DIR/versions/$RELEASE_ID"

printf 'Preparando una copia limpia del código fuente…\n'
if ! prepare_build_source "$BUILD_SOURCE_DIR"; then
  echo 'No fue posible preparar el código fuente temporal de Styler.' >&2
  exit 1
fi

printf 'Preparando Styler %s en una versión aislada…\n' "$PROJECT_VERSION"
if ! create_venv "$NEW_RELEASE_DIR"; then
  echo 'Falló la creación del entorno nuevo. La instalación anterior permanece intacta.' >&2
  exit 1
fi

# Styler 0.13.1 no vendoriza el source de PipeCraft, pero la distribución oficial
# incluye un binario privado por arquitectura. El instalador prefiere:
#   1) runtime PipeCraft incluido en este bundle;
#   2) PIPECRAFT_BIN para desarrollo/override;
#   3) `pipecraft` disponible en PATH;
#   4) PIPECRAFT_SOURCE_DIR como último recurso de desarrollo.
PIPECRAFT_REQUIRED_VERSION="1.5.0-alpha.1"
PIPECRAFT_EXTERNAL_BIN="${PIPECRAFT_BIN:-}"
PIPECRAFT_EXTERNAL_SOURCE="${PIPECRAFT_SOURCE_DIR:-}"
case "$(uname -m)" in
  x86_64|amd64) PIPECRAFT_BUNDLE_ARCH="linux-x86_64" ;;
  aarch64|arm64) PIPECRAFT_BUNDLE_ARCH="linux-aarch64" ;;
  *) PIPECRAFT_BUNDLE_ARCH="" ;;
esac
PIPECRAFT_BUNDLED_BIN=""
if [[ -n "$PIPECRAFT_BUNDLE_ARCH" ]]; then
  PIPECRAFT_BUNDLED_BIN="$SOURCE_DIR/runtime/pipecraft/$PIPECRAFT_BUNDLE_ARCH/pipecraft"
fi

if [[ -n "$PIPECRAFT_BUNDLED_BIN" && -f "$PIPECRAFT_BUNDLED_BIN" ]]; then
  PIPECRAFT_BUNDLED_SHA="$SOURCE_DIR/runtime/pipecraft/$PIPECRAFT_BUNDLE_ARCH/pipecraft.sha256"
  if [[ ! -f "$PIPECRAFT_BUNDLED_SHA" ]]; then
    echo 'ERROR: falta el checksum SHA-256 del runtime PipeCraft incluido.' >&2
    exit 1
  fi
  PIPECRAFT_EXPECTED_SHA="$(awk 'NF {print $1; exit}' "$PIPECRAFT_BUNDLED_SHA")"
  PIPECRAFT_ACTUAL_SHA="$(sha256sum "$PIPECRAFT_BUNDLED_BIN" | awk '{print $1}')"
  if [[ -z "$PIPECRAFT_EXPECTED_SHA" || "$PIPECRAFT_ACTUAL_SHA" != "$PIPECRAFT_EXPECTED_SHA" ]]; then
    echo 'ERROR: el runtime PipeCraft incluido está dañado o no coincide con su SHA-256.' >&2
    exit 1
  fi
  printf 'Runtime PipeCraft incluido verificado por SHA-256.\n'
  printf 'Instalando runtime PipeCraft incluido (%s)…\n' "$PIPECRAFT_BUNDLE_ARCH"
  install -m 0755 "$PIPECRAFT_BUNDLED_BIN" "$NEW_RELEASE_DIR/bin/pipecraft"
elif [[ -n "$PIPECRAFT_EXTERNAL_BIN" && -x "$PIPECRAFT_EXTERNAL_BIN" ]]; then
  printf 'Instalando binario PipeCraft indicado por PIPECRAFT_BIN: %s\n' "$PIPECRAFT_EXTERNAL_BIN"
  install -m 0755 "$PIPECRAFT_EXTERNAL_BIN" "$NEW_RELEASE_DIR/bin/pipecraft"
elif command -v pipecraft >/dev/null 2>&1; then
  PIPECRAFT_PATH_BIN="$(command -v pipecraft)"
  printf 'Copiando PipeCraft existente al release aislado: %s\n' "$PIPECRAFT_PATH_BIN"
  install -m 0755 "$PIPECRAFT_PATH_BIN" "$NEW_RELEASE_DIR/bin/pipecraft"
elif [[ -n "$PIPECRAFT_EXTERNAL_SOURCE" && -f "$PIPECRAFT_EXTERNAL_SOURCE/Cargo.toml" ]] && command -v cargo >/dev/null 2>&1; then
  printf 'Compilando PipeCraft desde checkout externo %s…\n' "$PIPECRAFT_EXTERNAL_SOURCE"
  cargo build --release --manifest-path "$PIPECRAFT_EXTERNAL_SOURCE/Cargo.toml" -p pipecraft-cli
  PIPECRAFT_BUILT_BIN="$PIPECRAFT_EXTERNAL_SOURCE/target/release/pipecraft"
  [[ -x "$PIPECRAFT_BUILT_BIN" ]] || { echo 'Cargo no produjo pipecraft.' >&2; exit 1; }
  install -m 0755 "$PIPECRAFT_BUILT_BIN" "$NEW_RELEASE_DIR/bin/pipecraft"
else
  cat >&2 <<'EOF'
ERROR: esta distribución no contiene un runtime PipeCraft compatible con la
arquitectura actual. Las operaciones con efectos no pueden ejecutarse.
Usa el bundle oficial de Styler para tu arquitectura o, para desarrollo, define
PIPECRAFT_BIN/PIPECRAFT_SOURCE_DIR. No existe fallback productivo a Python.
EOF
  exit 1
fi

if ! PIPECRAFT_INSTALLED_VERSION="$($NEW_RELEASE_DIR/bin/pipecraft --version 2>/dev/null)"; then
  echo 'El runtime PipeCraft instalado no puede ejecutarse en esta máquina.' >&2
  exit 1
fi
if [[ -z "$PIPECRAFT_INSTALLED_VERSION" ]]; then
  echo 'El runtime PipeCraft instalado no reportó una versión válida.' >&2
  exit 1
fi
printf 'Runtime PipeCraft verificado: %s\n' "$PIPECRAFT_INSTALLED_VERSION"

PIP_ARGS=(--disable-pip-version-check)
if [[ -d "$SOURCE_DIR/wheelhouse" ]] && compgen -G "$SOURCE_DIR/wheelhouse/*.whl" >/dev/null; then
  PIP_ARGS+=(--no-index --find-links "$SOURCE_DIR/wheelhouse")
fi

if ! "$NEW_RELEASE_DIR/bin/python" -m pip install "${PIP_ARGS[@]}" "$BUILD_SOURCE_DIR"; then
  cat >&2 <<'EOF'
No fue posible instalar Styler y sus dependencias dentro del entorno nuevo.
La instalación anterior permanece intacta. Comprueba la conexión a Internet o
utiliza una distribución que incluya la carpeta wheelhouse.
EOF
  exit 1
fi

if ! "$NEW_RELEASE_DIR/bin/styler" --version >/dev/null 2>&1; then
  echo 'La verificación final de Styler falló. La instalación anterior permanece intacta.' >&2
  exit 1
fi

STAGE_LAUNCHER="$STAGE_DIR/styler-launcher"
STAGE_DESKTOP="$STAGE_DIR/styler.desktop"
STAGE_MIME="$STAGE_DIR/styler-package.xml"

cat > "$STAGE_LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$APP_DIR/current/bin/styler" "\$@"
EOF
chmod +x "$STAGE_LAUNCHER"

sed \
  -e "s|^Exec=.*|Exec=$BIN_HOME/styler %f|" \
  -e "s|^TryExec=.*|TryExec=$BIN_HOME/styler|" \
  "$SOURCE_DIR/packaging/linux/styler.desktop" > "$STAGE_DESKTOP"
cp "$SOURCE_DIR/packaging/linux/styler-package.xml" "$STAGE_MIME"

mkdir -p "$BIN_HOME" "$APPLICATIONS_DIR" "$MIME_DIR"
OLD_CURRENT_TARGET=""
if [[ -L "$APP_DIR/current" ]]; then
  OLD_CURRENT_TARGET="$(readlink "$APP_DIR/current")"
elif [[ -e "$APP_DIR/current" ]]; then
  echo "$APP_DIR/current existe pero no es un enlace administrado por Styler." >&2
  echo 'No se modificó la instalación.' >&2
  exit 1
fi

OLD_LAUNCHER="$BIN_HOME/styler.previous.$$"
OLD_DESKTOP="$APPLICATIONS_DIR/styler.desktop.previous.$$"
OLD_MIME="$MIME_DIR/styler-package.xml.previous.$$"

backup_existing() {
  local original="$1" backup="$2"
  if [[ -e "$original" || -L "$original" ]]; then
    rm -rf -- "$backup"
    mv -- "$original" "$backup"
  fi
}

restore_backup() {
  local original="$1" backup="$2"
  rm -rf -- "$original"
  if [[ -e "$backup" || -L "$backup" ]]; then
    mv -- "$backup" "$original"
  fi
}

backup_existing "$BIN_HOME/styler" "$OLD_LAUNCHER"
backup_existing "$APPLICATIONS_DIR/styler.desktop" "$OLD_DESKTOP"
backup_existing "$MIME_DIR/styler-package.xml" "$OLD_MIME"

CURRENT_NEW="$APP_DIR/current.new.$$"
rm -f -- "$CURRENT_NEW"
ln -s "$NEW_RELEASE_DIR" "$CURRENT_NEW"

ACTIVATION_OK=1
mv -Tf -- "$CURRENT_NEW" "$APP_DIR/current" || ACTIVATION_OK=0
if [[ $ACTIVATION_OK -eq 1 ]]; then
  install -m 0755 "$STAGE_LAUNCHER" "$BIN_HOME/styler" || ACTIVATION_OK=0
fi
if [[ $ACTIVATION_OK -eq 1 ]]; then
  install -m 0644 "$STAGE_DESKTOP" "$APPLICATIONS_DIR/styler.desktop" || ACTIVATION_OK=0
fi
if [[ $ACTIVATION_OK -eq 1 ]]; then
  install -m 0644 "$STAGE_MIME" "$MIME_DIR/styler-package.xml" || ACTIVATION_OK=0
fi

if [[ $ACTIVATION_OK -ne 1 ]]; then
  rm -f -- "$APP_DIR/current"
  if [[ -n "$OLD_CURRENT_TARGET" ]]; then
    ln -s "$OLD_CURRENT_TARGET" "$APP_DIR/current"
  fi
  restore_backup "$BIN_HOME/styler" "$OLD_LAUNCHER"
  restore_backup "$APPLICATIONS_DIR/styler.desktop" "$OLD_DESKTOP"
  restore_backup "$MIME_DIR/styler-package.xml" "$OLD_MIME"
  echo 'No fue posible activar la instalación nueva. Se restauró la anterior.' >&2
  exit 1
fi

RELEASE_ACTIVATED=1
rm -rf -- "$OLD_LAUNCHER" "$OLD_DESKTOP" "$OLD_MIME"

# Aparta un entorno monolítico previo solo después de activar la nueva versión.
# No se ejecuta desde él: se conserva temporalmente únicamente para rollback del instalador.
if [[ -d "$APP_DIR/venv" && ! -L "$APP_DIR/venv" ]]; then
  MIGRATED_VENV_DIR="$APP_DIR/versions/migrated-venv-$(date +%Y%m%d%H%M%S)-$$"
  mv -- "$APP_DIR/venv" "$MIGRATED_VENV_DIR" || true
fi
if [[ -n "$OLD_CURRENT_TARGET" && -d "$OLD_CURRENT_TARGET" ]]; then
  PREVIOUS_NEW="$APP_DIR/previous.new.$$"
  rm -f -- "$PREVIOUS_NEW"
  ln -s "$OLD_CURRENT_TARGET" "$PREVIOUS_NEW"
  mv -Tf -- "$PREVIOUS_NEW" "$APP_DIR/previous" || true
fi

# Conserva como máximo las versiones current y previous. Las demás son restos
# de actualizaciones anteriores y pueden eliminarse de forma segura.
CURRENT_REAL="$(readlink -f "$APP_DIR/current" 2>/dev/null || true)"
PREVIOUS_REAL="$(readlink -f "$APP_DIR/previous" 2>/dev/null || true)"
for release in "$APP_DIR"/versions/*; do
  [[ -e "$release" ]] || continue
  release_real="$(readlink -f "$release" 2>/dev/null || true)"
  if [[ "$release_real" != "$CURRENT_REAL" && "$release_real" != "$PREVIOUS_REAL" ]]; then
    rm -rf -- "$release"
  fi
done

command -v update-mime-database >/dev/null 2>&1 && update-mime-database "$DATA_HOME/mime" || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPLICATIONS_DIR" || true

configure_shell_path
configure_immediate_command

# El escaneo posterior se ejecuta con la versión ya instalada. Compara contra
# el punto previo, registra los cambios hechos por el instalador y convierte el
# estado final en la línea base persistente del usuario.
printf 'Registrando el estado final y creando la línea base…\n'
if ! "$APP_DIR/current/bin/styler" cli registry-install-post \
    --root "$REGISTRY_ROOT" --home "$HOME" >/dev/null 2>&1; then
  printf 'Aviso: Styler se instaló, pero el registro inicial se completará al abrirlo.\n' >&2
fi

printf '\nStyler quedó instalado correctamente.\n'
printf 'Ábrelo desde el menú de aplicaciones o ejecuta: %s/styler\n' "$BIN_HOME"
printf 'PATH de usuario configurado: %s\n' "$BIN_HOME"
printf 'El comando styler queda disponible inmediatamente cuando existe una ruta visible segura para el puente; las terminales nuevas usan ~/.local/bin directamente.\n'
