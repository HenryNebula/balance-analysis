"""Minimal .env loader (stdlib only, no dependencies).

Loads KEY=VALUE lines from a .env into os.environ without overriding
variables already set in the environment — exports win, .env is the
default. Comments (#) and blank lines are ignored; values may be quoted.

Default path: `./.env` relative to the current working directory (repo
checkouts run from the root; callers may pass an explicit path).
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path | None = None) -> Path:
    """Load .env (default: ./.env in the cwd). Returns the path examined."""
    env_path = path or Path.cwd() / ".env"
    if not env_path.exists():
        return env_path
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
    return env_path
