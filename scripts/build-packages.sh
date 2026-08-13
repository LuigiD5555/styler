#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-all}"

case "$TARGET" in
  deb) "$ROOT/scripts/build-deb.sh" ;;
  arch|pacman) "$ROOT/scripts/build-arch.sh" ;;
  rpm|zypper|dnf) "$ROOT/scripts/build-rpm.sh" ;;
  all)
    failures=0
    for script in build-deb.sh build-arch.sh build-rpm.sh; do
      if ! "$ROOT/scripts/$script"; then
        failures=$((failures + 1))
      fi
    done
    if (( failures )); then
      echo "Algunos formatos no pudieron construirse en este sistema. Usa build-in-containers.sh para compilarlos todos." >&2
      exit 1
    fi
    ;;
  *)
    echo "Uso: $0 [deb|arch|rpm|all]" >&2
    exit 2
    ;;
esac
