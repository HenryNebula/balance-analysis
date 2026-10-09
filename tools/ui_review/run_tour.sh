#!/usr/bin/env bash
# Run the whole ui-review-loop capture: wipe state, boot the app, shoot the
# empty state, seed, shoot the full tour. Usage: tools/ui_review/run_tour.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

export UV_CACHE_DIR=/media/ext4-data/cache/uv-cache
export UV_PROJECT_ENVIRONMENT=/media/ext4-data/venvs/balance-analysis-web-ui
PORT="${PORT:-8765}"
export BASE_URL="http://127.0.0.1:${PORT}"
export SHOTS_DIR="${SHOTS_DIR:-/tmp/ui-shots-stmtledger}"

mkdir -p "$SHOTS_DIR"
rm -f "$SHOTS_DIR"/*.png

rm -rf data/accounts data/runs data/cache
uv run --group web uvicorn web.app:app --host 127.0.0.1 --port "$PORT" \
  > /tmp/ui-review-uvicorn.log 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  curl -sf "$BASE_URL/" >/dev/null && break
  sleep 0.5
done

node tools/ui_review/tour.mjs --phase=empty
uv run --group web python tools/ui_review/seed.py
node tools/ui_review/tour.mjs --phase=main

echo "shots in $SHOTS_DIR:"
ls "$SHOTS_DIR"
