#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-all}"
ENGINE="${CONTAINER_ENGINE:-}"
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"
if [[ -z "$ENGINE" ]]; then
  if command -v podman >/dev/null 2>&1; then
    ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then
    ENGINE=docker
  else
    echo "Instala Podman o Docker para construir todos los formatos desde una sola distribución." >&2
    exit 2
  fi
fi
mkdir -p "$ROOT/dist/packages"

run_deb() {
  "$ENGINE" run --rm \
    -e HOST_UID="$HOST_UID" -e HOST_GID="$HOST_GID" \
    -v "$ROOT:/src:ro,Z" -v "$ROOT/dist:/out:Z" \
    debian:13 bash -lc '
      set -e
      apt-get update
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential devscripts debhelper dh-python pybuild-plugin-pyproject fakeroot \
        python3-all python3-setuptools python3-wheel python3-pytest python3-pytest-asyncio \
        python3-textual python3-yaml
      cp -a /src /work
      cd /work
      ./scripts/build-deb.sh
      mkdir -p /out/packages/deb
      cp -a dist/packages/deb/. /out/packages/deb/
      chown -R "$HOST_UID:$HOST_GID" /out/packages/deb'
}

run_arch() {
  "$ENGINE" run --rm \
    -e HOST_UID="$HOST_UID" -e HOST_GID="$HOST_GID" \
    -v "$ROOT:/src:ro,Z" -v "$ROOT/dist:/out:Z" \
    archlinux:latest bash -lc '
      set -e
      pacman -Syu --noconfirm --needed base-devel python python-build python-installer \
        python-setuptools python-wheel python-pytest python-pytest-asyncio python-textual python-yaml
      useradd -m builder
      cp -a /src /work
      chown -R builder:builder /work
      runuser -u builder -- bash -lc "cd /work && ./scripts/build-arch.sh"
      mkdir -p /out/packages/arch
      cp -a /work/dist/packages/arch/. /out/packages/arch/
      chown -R "$HOST_UID:$HOST_GID" /out/packages/arch'
}

run_rpm() {
  "$ENGINE" run --rm \
    -e HOST_UID="$HOST_UID" -e HOST_GID="$HOST_GID" \
    -v "$ROOT:/src:ro,Z" -v "$ROOT/dist:/out:Z" \
    opensuse/tumbleweed bash -lc '
      set -e
      zypper --non-interactive refresh
      zypper --non-interactive install \
        rpm-build python3-devel python3-setuptools python3-wheel python3-build python3-installer \
        python3-pytest python3-pytest-asyncio python3-textual python3-PyYAML
      cp -a /src /work
      cd /work
      ./scripts/build-rpm.sh
      mkdir -p /out/packages/rpm
      cp -a dist/packages/rpm/. /out/packages/rpm/
      chown -R "$HOST_UID:$HOST_GID" /out/packages/rpm'
}

case "$TARGET" in
  deb) run_deb ;;
  arch|pacman) run_arch ;;
  rpm|zypper|dnf) run_rpm ;;
  all) run_deb; run_arch; run_rpm ;;
  *) echo "Uso: $0 [deb|arch|rpm|all]" >&2; exit 2 ;;
esac
