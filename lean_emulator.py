"""Lean probe library: frames as a DataFrame, the config form, nothing else.

The exploration layer is plain pandas/numpy — the model is a data
scientist, not an API caller. Group, pivot, cluster, regex, plot-histogram
your way through the layout; nothing here channels you. The only curated
objects are the engine's format tables (data for choosing *_formats ids)
and build_config — the distillation form whose kwargs are the config
fields. Convergence is unchanged: ONE full script, emu.build_config last,
engine gate verifies.

This module is copied into the run sandbox next to distill_emulator.py
(which supplies the form and the format tables — no duplication here).
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from distill_emulator import (  # noqa: F401 - re-exported for convenience
    AMOUNT_FORMAT_REGEXES,
    DATE_FORMAT_REGEXES,
    amount_format_histogram,
    amount_format_of,
    build_config,
    date_format_histogram,
    date_format_of,
    parse_amount,
    pattern_for,
    reconcile,
)


def frames() -> pd.DataFrame:
    """One row per printed item: text, x1, x2, cx (center), y, page, line.

    `line` is the visual-line index in reading order within the statement;
    y grows downward. This DataFrame IS the statement — everything else is
    your code. Typical moves:

        lines = df.groupby(["page", "line"]).agg(
            text=("text", " ".join), x1=("x1", "min"), x2=("x2", "max"))

        df.cx.hist(bins=80)            # column structure, visually
        df[df.line.between(a, b)]      # a table region
        df.text.str.contains(r"^\\d", regex=True)

    All statements passed to the run are included; `statement` names them.
    """
    data = json.loads(Path("frames.json").read_text())
    rows = []
    for statement in data["statements"]:
        for index, line in enumerate(statement["lines"]):
            for text, x1, x2 in line["items"]:
                rows.append(
                    {
                        "statement": statement["name"],
                        "text": text,
                        "x1": x1,
                        "x2": x2,
                        "cx": (x1 + x2) / 2,
                        "y": line["y"],
                        "page": line["page"],
                        "line": index,
                    }
                )
    return pd.DataFrame(rows)
