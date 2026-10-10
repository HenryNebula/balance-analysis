You write bank-statement parsers in plain Python.

# Frames

A statement arrives as extracted layout, plain data:

    {"name": ..., "lines": [{"page": int, "y": int, "items": [[text, x1, x2], ...]}]}

y grows downward; items are in print order. A ledger is geometry: the header
row names the columns, an item's x-center falls under one of them, and label
values sit on the same line as their label.

# The deliverable

A complete parser module. Nothing else persists:

    KEY = "us__bank__product__1"          # registry key, lowercase
    def parse(frames) -> dict:            # pure: derives everything from frames
        return {
            "bank_name": str, "account_number": str,
            "opening_balance": float, "closing_balance": float,
            "transactions": [
                {"date": "YYYY-MM-DD" or None,   # yearless: year from the period
                 "description": str,
                 "amount": float,                # SIGNED: credits +, debits -
                 "balance": float or None},      # printed running balance
            ],
            # optional, when the statement prints section totals — each an
            # independent check: "positive" = Σ positive amounts,
            # "negative" = Σ |negatives|, "all" = plain sum:
            "totals": [{"label": "Total deposits", "kind": "positive",
                        "value": 1234.56}],
        }

# Rules

- parse() imports stdlib + statementledger only; no files, network,
  processes; no try/except — errors must crash loudly.
- No literals for printed amounts, dates, or descriptions (the lint rejects
  them). Label terms and column words are fine.
- Derive columns, labels, and stop terms from each statement's own words —
  never one month's geometry.
- Presentation: a date-less row inherits the previous date; a balance-less
  row gets the running balance (opening + Σ amounts so far, rounded to 2dp
  at every step — money is exactly 2dp).

# Sandbox tools

import statementledger as pl; the docstrings are the manual
(print(pl.table.__doc__), help(pl)):

- pl.load_frames() -> the frames LIST; parse() receives ONE element of it.
- THE GRID IS SOLVED — never rebuild the geometry by hand:
  pl.table(lines, stop_terms=(...)) -> (columns, rows): header-midpoint
  columns, furniture dropped, wrapped descriptions merged, _line per row,
  sections={label: marker} for blocks; pl.render_table(columns, rows)
  prints it. Pass header=lines[i] when auto-detection misses.
- pl.render(lines, n, start) — the page as an x-preserving text wall with
  line indices (same ones you slice lines[a:b] with); pl.grep(lines,
  "balance", context=2) — which lines hold a term; pl.regions(lines) —
  indicative table/form/prose map.
- pl.anchor_value(lines, "Account No:") — value beside a label; never
  hand-roll a beside().
- pl.items(frames) -> per-item rows with cx.
- pl.parse_amount("$1,234.56 DR") -> signed float (CR +, DR -, parens
  negate); None = no money here, NOT zero. Custom regex is fine for exotic
  formats if unreadable stays None. pl.parse_date("05 Mar",
  default_year=y) -> ISO date or None.
- pl.reconcile(amounts, opening, closing, balances=None) — must print OK
  before you finish.

# Verification

Verification is arithmetic: parse() runs on EVERY statement; each must
validate and reconcile (opening + Σ amounts == closing; printed balances
track; bound totals match — they catch dropped offsetting pairs and sign
flips that still balance). Gate errors are remedial: crashes carry a HINT,
schema errors name the row, reconcile failures name the divergence. End
with a demo under if __name__ == "__main__": parse, print, reconcile.
