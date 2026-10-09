"""The parser registry: reuse with zero LLM.

Each registry module lives at statementledger/registry/<key>.py and defines

    KEY = "us__bank__product__1"
    def parse(frames) -> dict: ...

Modules are loaded by key, checked for the contract, and shipped in the
wheel — a registered parser parses FUTURE months of the same bank with no
LLM in the loop. A new bank means one new module (see tools/distill_harness.py).
"""

from __future__ import annotations

import re
from pathlib import Path

REGISTRY = Path(__file__).parent / "registry"
KEY_RE = re.compile(r"^[a-z]{2}__[a-z0-9_]+__[a-z0-9_]+__\d+$")


def keys() -> list[str]:
    """All registered parser keys, sorted."""
    return sorted(path.stem for path in REGISTRY.glob("*.py"))


def load_module(key: str):
    """Import registry/<key>.py and check it declares KEY + parse()."""
    import importlib.util

    if not KEY_RE.match(key):
        raise ValueError(f"bad key {key!r} (want cc__bank__product__n)")
    path = REGISTRY / f"{key}.py"
    if not path.exists():
        raise FileNotFoundError(f"no registered parser {key!r} in {REGISTRY}")
    spec = importlib.util.spec_from_file_location(
        f"statementledger_registry_{key}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "KEY", None) != key or not callable(getattr(module, "parse", None)):
        raise ValueError(f"{path} must define KEY = {key!r} and def parse(frames)")
    return module
