#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$($ROOT/scripts/project-version.py)"
OUT_DIR="${1:-$ROOT/dist/runtime}"
OUT="$OUT_DIR/styler-$VERSION.pyz"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v python3 >/dev/null 2>&1 || {
  echo "Falta Python 3." >&2
  exit 2
}
python3 -m venv "$WORK/venv" 2>/dev/null || {
  echo "No se pudo crear un entorno virtual. Instala python3-venv o el paquete equivalente." >&2
  exit 2
}
"$WORK/venv/bin/python" -m pip install --disable-pip-version-check --quiet --upgrade pip
"$WORK/venv/bin/python" -m pip install --disable-pip-version-check --quiet \
  --target "$WORK/app" -r "$ROOT/packaging/runtime/requirements.txt"

cp -a "$ROOT/styler" "$WORK/app/styler"
cat > "$WORK/app/__main__.py" <<'PY'
from styler.launcher import main
raise SystemExit(main())
PY

# El runtime debe ser Python puro para que el mismo archivo funcione en x86_64,
# ARM64 y otras arquitecturas. PyYAML conserva su implementación Python cuando
# se retira la extensión C opcional.
find "$WORK/app" -type f \( -name '*.so' -o -name '*.pyd' -o -name '*.dll' \) -delete
find "$WORK/app" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$WORK/app" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

mkdir -p "$OUT_DIR"
python3 -m zipapp "$WORK/app" --compress --python '/usr/bin/env python3' --output "$OUT"
chmod 0755 "$OUT"

PYTHONNOUSERSITE=1 python3 "$OUT" --help >/dev/null
PYTHONNOUSERSITE=1 python3 - "$OUT" <<'PY_YAML'
import sys
from pathlib import Path

archive = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(archive))
import yaml
assert yaml.safe_load("enabled: true") == {"enabled": True}
PY_YAML
PYTHONNOUSERSITE=1 python3 - "$OUT" <<'PY'
import asyncio
import sys
import tempfile
from pathlib import Path

archive = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(archive))
from styler.tui.app import HomeScreen, StylerApp

async def smoke() -> None:
    with tempfile.TemporaryDirectory() as root:
        app = StylerApp(root=root, demo=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, HomeScreen)

asyncio.run(smoke())
PY
sha256sum "$OUT" > "$OUT.sha256"
printf '%s\n' "$OUT"
