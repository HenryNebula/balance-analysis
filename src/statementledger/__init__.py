"""statementledger — bank statement PDFs to VERIFIED ledgers.

The idea: a statement is a ledger, and a ledger is verifiable — opening +
signed amounts must equal closing, and printed running balances must track.
Instead of one generic extractor guessing at every bank, each supported bank
gets a tiny pure-Python parse() function (a registry module) that reads the
extracted layout and returns the canonical statement dict. Verification is
arithmetic, not vibes: a parser ships only if its output reconciles.

    import statementledger as sl
    stmt = sl.parse("statement.pdf", key="us__bank__product__1")
    # or discover: stmt = sl.parse("statement.pdf")

A statement arrives as FRAMES — extracted layout, plain data
(statementledger.frames):

    {"name": ..., "lines": [{"page": int, "y": int,
                             "items": [[text, x1, x2], ...]}, ...]}

Writing a parser for a new bank is a REPL loop with an LLM (or by hand): the
distill harness in tools/distill_harness.py probes the frames, drafts
parse(), and gates every round on validate + reconcile — nothing is
registered until it reconciles.

Public API here; the engine internals live in .engine, PDF extraction in
.frames, the parser registry in .registry.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from statementledger.engine import (TOL, CANONICAL_FIELDS, VerificationError,
                                    evaluate, hint_for, items, line_text,
                                    parse_amount, parse_date, reconcile,
                                    reconcile_statement, show, validate)
from statementledger.frames import frames_for, load_frames
from statementledger.registry import keys, load_module

__version__ = "0.1.0"

__all__ = [
    "parse", "keys",
    # frames
    "frames_for", "load_frames",
    # engine
    "parse_amount", "parse_date", "reconcile", "reconcile_statement",
    "validate", "evaluate", "hint_for", "line_text", "show", "items",
    "VerificationError", "CANONICAL_FIELDS", "TOL",
    "__version__",
]


def parse(pdf: str, key: str | None = None) -> dict:
    """Parse one PDF with a registered parser (all of them when key is None;
    the first that verifies wins). Returns the canonical statement dict
    (tagged with its "_key"), raising VerificationError when none
    reconciles — that is the engine."""
    pdfs = [Path(pdf)]

    with tempfile.TemporaryDirectory() as tmp:
        frames = frames_for(pdfs, Path(tmp))
    statements = frames["statements"]
    candidates = [key] if key else keys()
    failures = []
    for candidate in candidates:
        module = load_module(candidate)
        verdict = evaluate(module.parse, statements)
        if verdict["verified"]:
            stmt = module.parse(statements[0])
            stmt["_key"] = candidate
            return stmt
        failures.extend(f"{candidate}: {r['error']}" for r in verdict["results"])
    raise VerificationError(
        f"no registered parser verified {Path(pdf).name}:\n" + "\n".join(failures))
