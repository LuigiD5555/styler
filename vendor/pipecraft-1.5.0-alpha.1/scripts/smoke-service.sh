#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BIN=${PIPECRAFT_BIN:-"$ROOT_DIR/target/release/pipecraft"}

if [ ! -x "$BIN" ]; then
  echo "PipeCraft binary not found: $BIN" >&2
  exit 1
fi

TMP=$(mktemp -d)
PID=""
cleanup() {
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    kill -INT "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

mkdir -p "$TMP/.pipelines/pipelines"
cat > "$TMP/.pipelines/workspace.yaml" <<'YAML'
schema_version: pipecraft/v1
workspace:
  name: service-smoke
paths:
  pipeline_dir: .pipelines/pipelines
  route_file: .pipelines/routes.yaml
  runs_dir: .pipelines/runs
defaults:
  default_pipeline: demo
YAML
cat > "$TMP/.pipelines/pipelines/demo.yaml" <<'YAML'
schema_version: pipecraft/v1
name: demo
steps:
  - id: hello
    type: note
    description: IPC smoke test
YAML

"$BIN" --root "$TMP" serve --worker-threads 2 --max-pipelines 2 --max-tasks 4 >"$TMP/service.log" 2>&1 &
PID=$!

i=0
while [ ! -S "$TMP/.pipelines/pipecraft.sock" ]; do
  if ! kill -0 "$PID" 2>/dev/null; then
    cat "$TMP/service.log" >&2
    exit 1
  fi
  i=$((i + 1))
  if [ "$i" -gt 100 ]; then
    echo "runtime service socket did not appear" >&2
    exit 1
  fi
  sleep 0.05
done

SUBMIT=$($BIN --root "$TMP" submit demo --json)
RUN_ID=$(printf '%s' "$SUBMIT" | python -c 'import json,sys; print(json.load(sys.stdin)["data"]["run_id"])')

status=""
i=0
while :; do
  STATUS_JSON=$($BIN --root "$TMP" status "$RUN_ID" --json)
  status=$(printf '%s' "$STATUS_JSON" | python -c 'import json,sys; print(json.load(sys.stdin)["data"]["status"])')
  case "$status" in
    succeeded) break ;;
    failed|cancelled|interrupted)
      printf '%s\n' "$STATUS_JSON" >&2
      exit 1
      ;;
  esac
  i=$((i + 1))
  if [ "$i" -gt 200 ]; then
    echo "service job did not finish" >&2
    exit 1
  fi
  sleep 0.05
done

$BIN --root "$TMP" job-report "$RUN_ID" --json >/dev/null
[ -f "$TMP/.pipelines/runtime/jobs/$RUN_ID.json" ]
[ -f "$TMP/.pipelines/runs/$RUN_ID/state.json" ]
[ -f "$TMP/.pipelines/runs/$RUN_ID/report.json" ]
[ -f "$TMP/.pipelines/runs/$RUN_ID/pipeline.snapshot.yaml" ]

kill -INT "$PID"
wait "$PID"
PID=""
[ ! -S "$TMP/.pipelines/pipecraft.sock" ]

echo "PipeCraft service smoke test passed."
