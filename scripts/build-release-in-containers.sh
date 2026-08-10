#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-all}"
ENGINE="${CONTAINER_ENGINE:-}"
VERSION="$($ROOT/scripts/project-version.py)"
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

if [[ -z "$ENGINE" ]]; then
  if command -v podman >/dev/null 2>&1; then
    ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then
    ENGINE=docker
  else
    echo "Instala Podman o Docker para construir todos los formatos." >&2
    exit 2
  fi
fi

mkdir -p "$ROOT/dist/runtime" "$ROOT/dist/packages"

build_runtime() {
  "$ENGINE" run --rm \
    -e HOST_UID="$HOST_UID" -e HOST_GID="$HOST_GID" \
    -v "$ROOT:/src:ro,Z" -v "$ROOT/dist:/out:Z" \
    python:3.12-slim bash -lc '
      set -e
      cp -a /src /work
      cd /work
      ./scripts/build-portable-runtime.sh /out/runtime
      chown -R "$HOST_UID:$HOST_GID" /out/runtime'
}

ensure_runtime() {
  [[ -f "$ROOT/dist/runtime/styler-$VERSION.pyz" ]] || build_runtime
}

build_deb() {
  ensure_runtime
  "$ENGINE" run --rm \
    -e HOST_UID="$HOST_UID" -e HOST_GID="$HOST_GID" \
    -v "$ROOT:/src:ro,Z" -v "$ROOT/dist:/out:Z" \
    debian:13 bash -lc '
      set -e
      apt-get update
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends dpkg-dev ca-certificates
      cp -a /src /work
      mkdir -p /work/dist/runtime
      cp /out/runtime/*.pyz /work/dist/runtime/
      cd /work
      ./scripts/build-release-deb.sh
      mkdir -p /out/packages/deb
      cp -a dist/packages/deb/. /out/packages/deb/
      chown -R "$HOST_UID:$HOST_GID" /out/packages/deb'
}

build_arch() {
  ensure_runtime
  "$ENGINE" run --rm \
    -e HOST_UID="$HOST_UID" -e HOST_GID="$HOST_GID" \
    -v "$ROOT:/src:ro,Z" -v "$ROOT/dist:/out:Z" \
    archlinux:latest bash -lc '
      set -e
      pacman -Syu --noconfirm --needed base-devel python ca-certificates
      useradd -m builder
      cp -a /src /work
      mkdir -p /work/dist/runtime
      cp /out/runtime/*.pyz /work/dist/runtime/
      chown -R builder:builder /work
      runuser -u builder -- bash -lc "cd /work && ./scripts/build-release-arch.sh"
      mkdir -p /out/packages/arch
      cp -a /work/dist/packages/arch/. /out/packages/arch/
      chown -R "$HOST_UID:$HOST_GID" /out/packages/arch'
}

build_rpm() {
  ensure_runtime
  "$ENGINE" run --rm \
    -e HOST_UID="$HOST_UID" -e HOST_GID="$HOST_GID" \
    -v "$ROOT:/src:ro,Z" -v "$ROOT/dist:/out:Z" \
    opensuse/tumbleweed bash -lc '
      set -e
      zypper --non-interactive refresh
      zypper --non-interactive install rpm-build python3 ca-certificates
      cp -a /src /work
      mkdir -p /work/dist/runtime
      cp /out/runtime/*.pyz /work/dist/runtime/
      cd /work
      ./scripts/build-release-rpm.sh
      mkdir -p /out/packages/rpm
      cp -a dist/packages/rpm/. /out/packages/rpm/
      chown -R "$HOST_UID:$HOST_GID" /out/packages/rpm'
}

case "$TARGET" in
  runtime) build_runtime ;;
  deb) build_deb ;;
  arch|pacman) build_arch ;;
  rpm|zypper|dnf) build_rpm ;;
  all) build_runtime; build_deb; build_arch; build_rpm ;;
  *) echo "Uso: $0 [runtime|deb|arch|rpm|all]" >&2; exit 2 ;;
esac
