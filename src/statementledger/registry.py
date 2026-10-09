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


def keys() -> list[str]:
    """All registered parser keys, sorted."""
    return sorted(path.stem for path in REGISTRY.glob("*.py"))


def load_module(key: str):
    """Import registry/<key>.py and check it declares KEY + parse()."""
    import importlib.util

    problem = key_problem(key)
    if problem:
        raise ValueError(problem)
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
