#!/usr/bin/env bash
# Submit the tour to the external reviewer (agy, headless) and print its
# verdict. Usage: review.sh <prompt-file> <output-file> [conversation-id]
# Runs with cwd = the shots dir (reviewer-setup.md); prompts reference the
# PNGs by absolute path, so only the cwd changes.
set -euo pipefail
SHOTS="${SHOTS_DIR:-/tmp/ui-shots-stmtledger}"
PROMPT="$(readlink -f "$1")"
OUT="$(readlink -f "$2")"
CONV="${3:-}"

cd "$SHOTS"
if [ -n "$CONV" ]; then
  agy --conversation "$CONV" --print "$(cat "$PROMPT")" > "$OUT" 2>&1
else
  agy --print "$(cat "$PROMPT")" > "$OUT" 2>&1
fi
echo "review written to $OUT ($(wc -l < "$OUT") lines)"
