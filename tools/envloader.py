"""Minimal .env loader (stdlib only, no dependencies).

Loads KEY=VALUE lines from .env next to this file into os.environ without
overriding variables already set in the environment — exports win, .env is
the default. Comments (#) and blank lines are ignored; values may be quoted.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path | None = None) -> Path:
    """Load .env (default: next to this module). Returns the path examined."""
    env_path = path or Path(__file__).parent / ".env"
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
