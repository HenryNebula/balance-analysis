#!/usr/bin/env bash
# Serve the statementledger web UI with durable, repo-independent storage.
#
# Accounts, statements, runs AND parsers distilled at runtime all live under
# STMTLEDGER_WEB_DATA (default ~/.local/share/statementledger) — user data,
# never inside the repository (the shipped registry stays curated source).
# HOST=0.0.0.0 exposes it to the LAN; remember there is deliberately no auth.
set -euo pipefail
cd "$(dirname "$0")/.."

export STMTLEDGER_WEB_DATA="${STMTLEDGER_WEB_DATA:-$HOME/.local/share/statementledger}"
exec uv run --group web uvicorn web.app:app \
  --host "${HOST:-127.0.0.1}" --port "${PORT:-8765}"
