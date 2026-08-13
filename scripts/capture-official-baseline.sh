#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Uso:
  capture-official-baseline.sh ID NOMBRE IMAGEN CHECKSUM [PERFIL] [DESTINO]

Ejemplo:
  ./scripts/capture-official-baseline.sh \
    linuxmint-22.1-cinnamon-x86_64 \
    "Linux Mint 22.1 Cinnamon x86_64" \
    linuxmint-22.1-cinnamon-64bit.iso \
    sha256:... \
    default-desktop \
    ./linuxmint-22.1-cinnamon-x86_64.stylerpkg

Ejecuta esto únicamente en una instalación limpia de la distribución, después
de instalar el runtime mínimo y Styler. El script no limpia el sistema ni puede
comprobar por sí mismo que la instalación sea nueva.
EOF
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" || $# -lt 4 ]]; then
  usage
  [[ $# -ge 4 ]] || exit 2
fi

baseline_id=$1
name=$2
image_name=$3
image_checksum=$4
profile=${5:-default-desktop}
destination=${6:-"./${baseline_id}.stylerpkg"}

printf '%s\n' "Se capturará una línea base OFICIAL desde el estado actual." >&2
printf '%s\n' "Confirma que la distro está recién instalada y contiene solo su estado original + runtime Styler." >&2
read -r -p "Escribe CAPTURAR para continuar: " confirmation
if [[ $confirmation != "CAPTURAR" ]]; then
  printf '%s\n' "Operación cancelada." >&2
  exit 1
fi

styler baseline-capture \
  --kind official \
  --id "$baseline_id" \
  --name "$name" \
  --clean-install \
  --installation-profile "$profile" \
  --image-name "$image_name" \
  --image-checksum "$image_checksum" \
  --updates-policy all-updates-before-capture \
  --after-updates \
  --author "Styler project" \
  --export "$destination"
