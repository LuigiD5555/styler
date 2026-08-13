#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-all}"
case "$TARGET" in
  deb) "$ROOT/scripts/build-release-deb.sh" ;;
  arch|pacman) "$ROOT/scripts/build-release-arch.sh" ;;
  rpm|zypper|dnf) "$ROOT/scripts/build-release-rpm.sh" ;;
  all)
    "$ROOT/scripts/build-portable-runtime.sh"
    "$ROOT/scripts/build-release-deb.sh"
    "$ROOT/scripts/build-release-arch.sh"
    "$ROOT/scripts/build-release-rpm.sh"
    ;;
  *) echo "Uso: $0 [deb|arch|rpm|all]" >&2; exit 2 ;;
esac
