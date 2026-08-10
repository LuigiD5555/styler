#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGES="${1:-$ROOT/dist/packages}"
OUT="${2:-$ROOT/dist/repositories}"
GPG_KEY="${STYLER_GPG_KEY:-}"

rm -rf "$OUT"
mkdir -p "$OUT"

if compgen -G "$PACKAGES/deb/*.deb" >/dev/null; then
  command -v dpkg-scanpackages >/dev/null 2>&1 || {
    echo "Falta dpkg-scanpackages (paquete dpkg-dev)." >&2; exit 2;
  }
  APT="$OUT/apt"
  mkdir -p "$APT/pool/main/s/styler"
  cp "$PACKAGES"/deb/*.deb "$APT/pool/main/s/styler/"
  for arch in amd64 arm64; do
    DIR="$APT/dists/stable/main/binary-$arch"
    mkdir -p "$DIR"
    (cd "$APT" && dpkg-scanpackages --multiversion pool /dev/null) > "$DIR/Packages"
    gzip -9 -c "$DIR/Packages" > "$DIR/Packages.gz"
  done
  "$ROOT/scripts/create-apt-release.py" "$APT"
  if [[ -n "$GPG_KEY" ]]; then
    gpg --batch --yes --local-user "$GPG_KEY" --clearsign \
      --output "$APT/dists/stable/InRelease" "$APT/dists/stable/Release"
    gpg --batch --yes --local-user "$GPG_KEY" --armor --detach-sign \
      --output "$APT/dists/stable/Release.gpg" "$APT/dists/stable/Release"
    gpg --batch --yes --local-user "$GPG_KEY" --armor --export > "$APT/styler-repository.asc"
  fi
fi

if compgen -G "$PACKAGES/arch/*.pkg.tar.*" >/dev/null; then
  command -v repo-add >/dev/null 2>&1 || {
    echo "Falta repo-add (paquete pacman)." >&2; exit 2;
  }
  ARCH="$OUT/arch/x86_64"
  mkdir -p "$ARCH"
  cp "$PACKAGES"/arch/*.pkg.tar.* "$ARCH/"
  pushd "$ARCH" >/dev/null
  if [[ -n "$GPG_KEY" ]]; then
    for package in *.pkg.tar.*; do
      [[ "$package" == *.sig ]] && continue
      gpg --batch --yes --local-user "$GPG_KEY" --detach-sign "$package"
    done
    repo-add --sign --key "$GPG_KEY" styler.db.tar.gz ./*.pkg.tar.*
    gpg --batch --yes --local-user "$GPG_KEY" --armor --export > styler-repository.asc
  else
    repo-add styler.db.tar.gz ./*.pkg.tar.*
  fi
  ln -sf styler.db.tar.gz styler.db
  ln -sf styler.files.tar.gz styler.files
  popd >/dev/null
fi

if compgen -G "$PACKAGES/rpm/*.rpm" >/dev/null; then
  command -v createrepo_c >/dev/null 2>&1 || {
    echo "Falta createrepo_c." >&2; exit 2;
  }
  RPM="$OUT/rpm"
  mkdir -p "$RPM"
  cp "$PACKAGES"/rpm/*.rpm "$RPM/"
  createrepo_c "$RPM"
  if [[ -n "$GPG_KEY" ]]; then
    gpg --batch --yes --local-user "$GPG_KEY" --armor --detach-sign \
      "$RPM/repodata/repomd.xml"
    gpg --batch --yes --local-user "$GPG_KEY" --armor --export > "$RPM/styler-repository.asc"
  fi
fi

cat > "$OUT/README.txt" <<'EOF2'
Este árbol contiene repositorios estáticos para APT, pacman y RPM.
No publiques un repositorio sin firma para uso general. Define STYLER_GPG_KEY
antes de ejecutar create-repositories.sh y conserva la llave privada fuera del
repositorio de código.
EOF2
printf 'Repositorios creados en: %s\n' "$OUT"
