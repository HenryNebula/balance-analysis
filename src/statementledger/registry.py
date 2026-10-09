"""The parser registry: reuse with zero LLM.

Each registry module lives at statementledger/registry/<key>.py and defines

    KEY = "us__bank__product__1"
    def parse(frames) -> dict: ...

Modules are loaded by key, checked for the contract, and shipped in the
wheel — a registered parser parses FUTURE months of the same bank with no
LLM in the loop. A new bank means one new module (see tools/distill_harness.py).

Two layers, in precedence order:

1. The USER REGISTRY — an overlay directory (STATEMENTLEDGER_USER_REGISTRY,
   set by the web app to a data-tree path). Parsers distilled at runtime are
   USER DATA: they live under the app's data root, never inside the repo,
   and shadow same-key built-ins so a maintain run can evolve a shipped
   parser without touching package source.
2. The BUILT-IN registry (this package's registry/ dir) — the curated set
   that ships in the wheel.

keys() globs both live on every call, so a parser saved by the distill
harness becomes usable by a running web server with no restart.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REGISTRY = Path(__file__).parent / "registry"
KEY_RE = re.compile(r"^[a-z]{2}__[a-z0-9_]+__[a-z0-9_]+__\d+$")


def user_registry() -> Path | None:
    """The overlay directory (None when STATEMENTLEDGER_USER_REGISTRY is
    unset) — user-distilled parsers, shadowing the built-in registry."""
    path = os.environ.get("STATEMENTLEDGER_USER_REGISTRY", "").strip()
    return Path(path) if path else None


def keys() -> list[str]:
    """All registered parser keys, sorted (built-in + user overlay)."""
    found = {path.stem for path in REGISTRY.glob("*.py")}
    overlay = user_registry()
    if overlay is not None:
        found |= {path.stem for path in overlay.glob("*.py")}
    return sorted(found)


def module_path(key: str) -> Path:
    """Where a parser with this key lives — or should be SAVED: the overlay
    when one is configured (runtime-distilled parsers are user data), else
    the built-in registry (e.g. the distill harness run by hand from the
    repo root)."""
    overlay = user_registry()
    if overlay is not None:
        return overlay / f"{key}.py"
    return REGISTRY / f"{key}.py"


def load_module(key: str):
    """Import the parser module for a key (overlay shadows built-in) and
    check it declares KEY + parse()."""
    import importlib.util

    if not KEY_RE.match(key):
        raise ValueError(f"bad key {key!r} (want cc__bank__product__n)")
    path = module_path(key)
    if not path.exists():
        path = REGISTRY / f"{key}.py"  # fall back to the built-in copy
    if not path.exists():
        raise FileNotFoundError(
            f"no registered parser {key!r} (looked in {path} and {REGISTRY})")
    spec = importlib.util.spec_from_file_location(
        f"statementledger_registry_{key}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "KEY", None) != key or not callable(getattr(module, "parse", None)):
        raise ValueError(f"{path} must define KEY = {key!r} and def parse(frames)")
    return module
