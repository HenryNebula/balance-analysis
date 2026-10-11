"""The parser registry: reuse with zero LLM.

Each registry module lives at statementledger/registry/<key>.py and defines

    KEY = "us__bank__product__1"
    def parse(frames) -> dict: ...

Modules are loaded by key, checked for the contract, and shipped in the
wheel — a registered parser parses FUTURE months of the same bank with no
LLM in the loop. A new bank means one new module (see tools/distill_agent.py).

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
# Filler words from the harness's template key ("us__bank__product__1") —
# a real bank/product segment is never exactly one of these.
PLACEHOLDER_SEGMENTS = {"bank", "product", "demo"}


def key_problem(key: str) -> str | None:
    """Why `key` cannot be a registry key (None when it can). Beyond the
    KEY_RE shape: exactly four '__'-separated segments (the regex product
    segment would otherwise swallow 'bank__product'), and no unreplaced
    template placeholders left in the bank/product positions — the distill
    drafter has shipped keys like us__bank__liberty_national__credit__1."""
    if not KEY_RE.match(key):
        return f"bad key {key!r} (want cc__bank__product__n, lowercase)"
    segments = key.split("__")
    if len(segments) != 4:
        return (f"bad key {key!r}: {len(segments)} '__'-separated segments, "
                "want exactly 4 (cc__bank__product__n)")
    _cc, bank, product, _n = segments
    if bank in PLACEHOLDER_SEGMENTS or product in PLACEHOLDER_SEGMENTS:
        return (f"bad key {key!r}: the bank/product segments {bank!r}/{product!r} "
                "look like unreplaced template placeholders — derive them from "
                "the statement (e.g. sg__standard_chartered__savings__1)")
    return None


def user_registry(overlay: Path | None = None) -> Path | None:
    """The overlay directory — user-distilled parsers, shadowing the built-in
    registry. Default: STATEMENTLEDGER_USER_REGISTRY from the environment;
    `overlay` overrides it (the web layer pins each app's own data tree —
    the env var is process-global and wrong when several app instances
    coexist, e.g. in tests)."""
    if overlay is not None:
        return overlay
    path = os.environ.get("STATEMENTLEDGER_USER_REGISTRY", "").strip()
    return Path(path) if path else None


def keys(overlay: Path | None = None) -> list[str]:
    """All registered parser keys, sorted (built-in + user overlay)."""
    found = {path.stem for path in REGISTRY.glob("*.py")}
    overlay_dir = user_registry(overlay)
    if overlay_dir is not None:
        found |= {path.stem for path in overlay_dir.glob("*.py")}
    return sorted(found)


def module_path(key: str, overlay: Path | None = None) -> Path:
    """Where a parser with this key lives — or should be SAVED: the overlay
    when one is configured (runtime-distilled parsers are user data), else
    the built-in registry (e.g. the distill harness run by hand from the
    repo root)."""
    overlay_dir = user_registry(overlay)
    if overlay_dir is not None:
        return overlay_dir / f"{key}.py"
    return REGISTRY / f"{key}.py"


def load_module(key: str, overlay: Path | None = None):
    """Import the parser module for a key (overlay shadows built-in) and
    check it declares KEY + parse()."""
    import importlib.util

    problem = key_problem(key)
    if problem:
        raise ValueError(problem)
    path = module_path(key, overlay)
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
