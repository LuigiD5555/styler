#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${1:?usage: install-bundled-pipecraft.sh /path/to/pipecraft}"
[[ -x "$SRC" ]] || { echo "Not executable: $SRC" >&2; exit 2; }
case "$(uname -m)" in
  x86_64|amd64) ARCH=linux-x86_64 ;;
  aarch64|arm64) ARCH=linux-aarch64 ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 2 ;;
esac
DEST="$ROOT/runtime/pipecraft/$ARCH"
mkdir -p "$DEST"
install -m 0755 "$SRC" "$DEST/pipecraft"
SHA="$(sha256sum "$DEST/pipecraft" | awk '{print $1}')"
VERSION="$($DEST/pipecraft --version | awk '{print $NF}')"
cat > "$DEST/manifest.json" <<EOF
{
  "runtime": "pipecraft",
  "version": "$VERSION",
  "protocol": "pipecraft.ipc/v1",
  "target": "$ARCH",
  "sha256": "$SHA",
  "bundled_by": "styler 0.13.1"
}
EOF
python3 "$ROOT/scripts/verify-bundled-pipecraft.py"
