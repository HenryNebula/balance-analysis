"""REPL-distill harness for transtractor configs.

Paradigm shift vs config_harness.py: the model never emits a config JSON
directly and never sees a verdict-only loop. Instead each round it emits a
complete PROBE SCRIPT against the statement's layout frames (via
distill_emulator, which mirrors the engine's semantics). The harness runs
the script in a sandboxed subprocess and feeds back exactly what the script
printed — head-style views, format checks, a local reconcile — plus, once
the script calls emu.build_config(...), the real engine's evaluation of the
distilled config (same feedback machinery as the config harness).

Loop per round:
    model emits full python script (chat)
      -> AST lint (transcription guard: no money-shaped literals)
      -> run in sandboxed child (timeout, rlimits, no network, clean env)
      -> script printed probes + local reconcile?  -> stdout is the feedback
      -> script called emu.build_config(**fields)? -> config.json distilled
      -> engine gate: Parser(rules_only=True).load_str + parse ALL statements
      -> all clean? save to registry : build feedback and revise

The end state is unchanged from config_harness.py: a plain config in the
registry that parses future statements in milliseconds with zero LLM. The
scripts are scaffolding: kept under the run directory for audit, never
registered.

Usage (from balance-analysis/):
    uv run python distill_harness.py statement1.pdf [statement2.pdf ...]
    uv run python distill_harness.py --script canned_probe.py stmt.pdf
    uv run python distill_harness.py --py stmt1.pdf [stmt2.pdf ...]  # pyledger
    uv run python distill_harness.py --py --key us__bank__prod__1 old.pdf new.pdf
        # MAINTAIN: evolve the registered parser until ALL statements verify
        # (old ones are the regression set); starts from the registry source
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from envloader import load_env

load_env()

from config_harness import (  # noqa: E402 - after load_env, like llm_extract
    BASE_URL,
    FIELD_GUIDE,
    FORMAT_CATALOG,
    LAYOUT_ITEM_XY_RE,
    MODEL,
    REGISTRY,
    TIMEOUT,
    build_feedback,
    derive_alignments,
    evaluate,
    layout_dump,
)

SCRIPT_TIMEOUT = 60  # wall clock per probe script, seconds
OUTPUT_TAIL = 12000  # chars of script stdout/stderr fed back to the model
# (real statements are long; the model debugs through this window)
MAX_TOKENS = 10000  # probe scripts are longer than config JSON

EMU_API = """Working environment (your script runs with the statement frames
already on disk; import and go):
- import distill_emulator as emu
- THE EMULATOR IS INTROSPECTABLE at runtime: every function's full
  docstring and signature are one print away — print(emu.table.__doc__),
  print(inspect.signature(emu.classify)), or help(emu) in a probe. The
  summaries above are a map, not the manual.
- THE FRAMES ARE PLAIN DATA: lines is a list of {"page","y","items":
  [[text,x1,x2],...]} dicts. Every emu.* helper is a CONVENIENCE, not a
  gate — when a statement's shape doesn't fit one (wide multi-line columns,
  odd sections), drop to plain python/re on the frames and write your own
  grouping/clustering. Full freedom; the checks (reconcile, engine gate)
  stay deterministic.
- emu.tokens(lines) — flat (text,x1,x2,y,page,line_index) item stream.
- emu.cluster_x(items, gap=20) — 1-D x-center clustering into column groups.
- emu.date_format_histogram(texts) / emu.amount_format_histogram(texts) —
  {format id: [samples]} coverage of your actual tokens; choose *_formats
  ids from this evidence, never by guessing.
- emu.table(lines, stop_terms=[...], sections={label: marker}) — MATERIALIZE
  THE LEDGER GRID: columns from the header row, cells assigned by header
  span, page furniture dropped, wrapped rows merged, optional per-row
  "_section" label. THIS IS HOW YOU SEE THE TABLE — render_table() prints
  it. Never rebuild the geometry by hand.
- emu.render_table(columns, rows) — the grid as aligned text; read it.
- frames = emu.load_frames() -> [{name, lines}]; each line is
  {"page": int, "y": int, "items": [[text, x1, x2], ...]}; y grows downward.
- emu.show(lines, n=15, start=0) — print a slice of the layout with indices.
- emu.anchor_value(lines, term) — the printed value beside a label, one
  string ("Account number:" -> "1234 5678 9123 4567"). Use it for y1 anchors;
  never index lines/items structures by hand.
- emu.line_text(line); emu.lines_with(term) — lines containing a label term.
- emu.values_on_line(line, term) — texts printed right of the label (y1 anchor).
- emu.header_line(lines, *words, need=2); emu.spans(line) — the column header
  row and its {header: (x1, x2)} spans.
- emu.region(lines, start_term, stop_terms) — (first, last) indices of the
  transaction table.
- emu.classify(lines, column_spans, first, last) — assign items to columns by
  span center; returns (rows, orders). rows[i][role] is the list of printed
  texts for that role ([] on continuation rows); orders[i] is the row's role
  sequence — exactly the shapes transaction_formats needs.
- emu.amount_format_of(words) / emu.date_format_of(token) — which engine
  format id reads a printed token.
- emu.parse_amount(words, fmt) / emu.parse_date(token, year=None) — verify a
  format actually parses; CR is positive, DR negative.
- emu.reconcile(amounts, opening, closing) — the balance check, locally:
  ALWAYS run it on the full signed amount column before building the config.
- emu.signed_amounts(rows) — (credit - debit) per row, auto-parsed; rows
  without money contribute 0. Feed its output straight into reconcile().
- emu.pattern_for(token) — the shape-regex for a printed token (digit runs
  become \\d{n}); derive account_number_patterns from the value printed
  beside the label instead of inventing a pattern.
- emu.transaction_shapes(orders) — classify()'s role sequences turned into
  transaction_formats orders (debit/credit -> the single amount column,
  money-less continuation shapes dropped). Use its output verbatim.
- emu.build_config(**fields) — validate + write config.json; the LAST call.
  Keyword arguments are exactly the config fields below; invalid values
  raise with the valid options listed."""

EXAMPLE_SCRIPT = '''import distill_emulator as emu

frames = emu.load_frames()
lines = frames[0]["lines"]
emu.show(lines, 12)

# y1 anchors: read the PRINTED labels, then the value printed beside them
anchors = {label: emu.anchor_value(lines, label)
           for label in ("Opening balance:", "Closing balance:", "Account number:")}
print(anchors)
opening = emu.parse_amount(anchors["Opening balance:"], emu.amount_format_of(anchors["Opening balance:"]))
closing = emu.parse_amount(anchors["Closing balance:"], emu.amount_format_of(anchors["Closing balance:"]))

# materialize the ledger grid and READ it (sections optional, drop if absent)
columns, rows = emu.table(
    lines, stop_terms=["End of transactions"],
    sections={"debit": "Withdrawals and other subtractions",
              "credit": "Deposits and other additions"},
)
emu.render_table(columns, rows[:8])

# header row, spans, table region (use the words you saw in the show() output)
header = emu.header_line(lines, "Date", "Description", "Balance")
spans = emu.spans(header)
first, last = emu.region(lines, "Transaction History", ["End of transactions"])
rows, orders = emu.classify(
    lines,
    {"date": spans["Date"], "description": spans["Description"],
     "debit": spans.get("Debit"), "amount": spans.get("Credit"),
     "balance": spans.get("Balance")},
    first=lines.index(header) + 1,
    last=last,
)

# verify formats against REAL samples taken from the rows, reconcile
credit = next(r["amount"] for r in rows if r["amount"])
amount_fmt = emu.amount_format_of(credit)
balance_fmt = emu.amount_format_of(next(r["balance"] for r in rows if r["balance"]))
amounts = emu.signed_amounts(rows)
emu.reconcile(amounts, opening, closing)

# row shapes -> config orders (debit/credit map to "amount"; continuations drop)
transaction_formats = emu.transaction_shapes(orders)

emu.build_config(
    key="us__demo__checking__1",
    account_type="Checking",
    bank_name="Demo Bank",
    account_terms=["Demo Bank"],
    account_number_terms=["Account number:"],
    account_number_patterns=[emu.pattern_for(anchors["Account number:"])],
    account_number_alignment="y1",
    opening_balance_terms=["Opening balance:"],
    opening_balance_formats=[emu.amount_format_of(anchors["Opening balance:"])],
    opening_balance_alignment="y1",
    closing_balance_terms=["Closing balance:"],
    closing_balance_formats=[emu.amount_format_of(anchors["Closing balance:"])],
    closing_balance_alignment="y1",
    start_date_terms=["Statement Period:"],
    start_date_formats=["format2"],
    start_date_alignment="y1",
    transaction_terms=["Transaction History"],
    transaction_terms_stop=["End of transactions"],
    transaction_formats=transaction_formats,
    transaction_date_formats=["format1"],
    transaction_date_headers=["Date"],
    transaction_date_alignment="x1",
    transaction_description_headers=["Description"],
    transaction_description_alignment="x1",
    transaction_amount_formats=[amount_fmt],
    transaction_amount_headers=["Credit"],
    transaction_amount_alignment="x2",
    transaction_amount_invert_headers=["Debit"],
    transaction_amount_invert_alignment="x2",
    transaction_balance_formats=[balance_fmt],
    transaction_balance_headers=["Balance"],
    transaction_balance_alignment="x2",
)'''

CONTRACT = f"""Contract for every response:
- ONE complete runnable python script in a single ```python fence; shape it
  like the example below: probes -> verification -> emu.build_config last.
- KEEP IT SHORT: under ~60 lines. No helper functions, no boilerplate
  comments, no retry loops. The example is the maximum sensible size.
- NEVER use try/except: an error must crash the script — the harness feeds
  the exact traceback back to you. Swallowed errors hide the bug from you.
- Helpers are conveniences, not constraints: when the layout doesn't fit
  a helper (wide multi-line description columns, odd grouping), write
  plain python/re directly on the frames — tokens(), cluster_x(), or your
  own loops — instead of forcing a shaped tool.
- Read what YOUR OWN probes printed: use the exact printed label words, not
  generic guesses like "Transactions" or "End of Statement".
- BURNER PROBES: to just LOOK at something before committing, return a short
  script whose first line is exactly "# probe" (print anything; NO
  emu.build_config call). Probes are discarded — nothing persists, and there
  is no cap on them — but they cannot converge the run. The converging
  answer is always ONE full script; from 90% of turns used onward you will
  be warned on every probe that only a full script can finish.
- MANDATORY LOOKING: the harness does NOT accept your first full script
  until burner probes have shown the TOP of the statement AND its END (the
  words that terminate the table are at the end — read them, never guess).
- NEVER call emu.build_config while emu.reconcile prints FAIL — a failing
  reconcile means the region/roles/formats are wrong (often the stop term
  cuts the table early). Fix it first; the drift names the problem.
- Literals are allowed only for terms/headers/format ids/regexes, never for
  printed amounts, dates or transaction descriptions (the lint rejects them;
  sample tokens from classified rows instead, like the example does)."""




SYSTEM_PROMPT = f"""You are a data analyst drafting a bank-statement parser
configuration by writing probe scripts against extracted layout frames. You
never transcribe statement values into the code — the script DERIVES every
value from the frames at runtime. Probing is free (print anything); the only
thing that persists is the final emu.build_config(**fields) call, whose
keyword arguments are exactly the engine's config fields.

{EMU_API}

{FIELD_GUIDE}

{FORMAT_CATALOG}

{CONTRACT}

A complete example script for a different (demo) bank — imitate its shape
and length, not its literal labels:

{EXAMPLE_SCRIPT}
"""

LEAN_EXAMPLE = '''import lean_emulator as le
import pandas as pd

df = le.frames()
print(df.head(15))                       # the statement, one row per item
print(df.groupby("page").size())

lines = df.groupby(["page", "line"]).agg(  # visual lines as text spans
    text=("text", " ".join), x1=("x1", "min"), x2=("x2", "max"),
).reset_index()
print(lines.head(30))
print(lines.tail(6))                     # find the words that END the table

# anchors: labels are lines; values sit right of them
opening_row = df[df.line == df[df.text.str.contains("Opening", regex=False)].line.iloc[0]]
print(opening_row)                       # read the value off the same line

# column structure from geometry (headers or clustering)
header_line = lines[lines.text.str.contains("Date") & lines.text.str.contains("Balance")].line.iloc[0]
head = df[df.line == header_line].sort_values("cx")
print(head[["text", "x1", "x2"]])        # column names and their spans

# the grid: assign items to the nearest header span, rows = lines
region = df[df.line > header_line]
region = region[region.line < lines[lines.text.str.contains("End of transactions", regex=False)].line.iloc[0]]
edges = [float("-inf")] + [ (a + b) / 2 for a, b in zip(head.cx, head.cx.tolist()[1:]) ] + [float("inf")]
grid = region.assign(col=pd.cut(region.cx, bins=edges, labels=head.text.tolist())
).pivot_table(index="line", columns="col", values="text", aggfunc=" ".join)
print(grid.head(12))

# formats from EVIDENCE: histograms of what the engine can actually read
amounts = grid.get("Credit").dropna().tolist() + grid.get("Debit").dropna().tolist()
print(le.amount_format_histogram(amounts))
print(le.date_format_histogram(grid.get("Date").dropna().tolist()))

le.build_config(
    key="us__demo__checking__1",
    # ... the same kwargs as the engine config: terms from the lines you
    # read, formats from the histograms, transaction_formats from the
    # grid's column-presence shapes, alignments from the header spans.
)'''

LEAN_SYSTEM_PROMPT = f"""You are a data analyst who parses bank statement
layouts with pandas. le.frames() hands you the statement as a DataFrame
(one row per printed item: text, x1, x2, cx, y, page, line; y grows
downward). Explore FREELY with pandas/numpy — group, cut, cluster, regex,
pivot; there is no API to obey, only data to understand.

The deliverable is unchanged: a transtractor config. The LAST line of your
full script is le.build_config(**fields) — kwargs are exactly the engine's
config fields, validated on the spot. Two helps: amount_format_histogram()
/ date_format_histogram() show which engine format ids cover your actual
tokens (pick ids from evidence, never guessing), and reconcile() mirrors
the engine's balance check locally.

{FIELD_GUIDE}

{FORMAT_CATALOG}

{CONTRACT}

Worked example (demo bank — imitate the pandas idiom, not its labels):
```python
{LEAN_EXAMPLE}
```"""


TASK_TEMPLATE = """Statement to parse: {name} ({pages} pages, {lines} layout
lines). frames.json in the working directory already holds it.

Write the probe script: find the label anchors (account number, opening/
closing balance, statement period), locate the column header row and its
spans, print the END of the statement (emu.show(lines, 6, len(lines) - 6))
to read the exact words that terminate the transaction table, slice the
table with those words, classify its rows, verify the printed amount/date
tokens parse under the formats you intend to declare, reconcile the signed
amounts against the printed opening/closing balances, then call
emu.build_config(...) with the verified parameters. Return the complete
script in one ```python fence.

emu.table() materializes the ledger into regular rows and columns —
probe with it and READ the rendered grid instead of reconstructing the
geometry by hand; verify amounts and reconcile against it.

When you are NOT yet sure what a label, header or stop word says, first
return a BURNER PROBE — a short script whose first line is exactly
"# probe" that just prints what you need to see, e.g.:

# probe
import distill_emulator as emu
lines = emu.load_frames()[0]["lines"]
emu.show(lines, 6, len(lines) - 6)

It is discarded; then return the complete full script with what you read."""


LEAN_TASK_TEMPLATE = """Statement to parse: {name} ({pages} pages, {lines} layout
lines). frames.json in the working directory holds it; le.frames() returns
it as a DataFrame.

Explore with pandas until the layout is clear (lines, header spans, the
grid, section boundaries if any, the words that end the table), verify the
amounts reconcile against the printed opening/closing balances, then
return the complete full script ending in le.build_config(...) with
parameters chosen from evidence. Return it in one ```python fence.

When unsure what a label or stop word says, first return a BURNER PROBE —
a short script whose first line is exactly "# probe" that prints what you
need (le.frames().head(), a groupby, a tail slice). Discarded; then the
full script."""


# --- py mode: the artifact IS the parser (pyledger engine) ----------------------

PY_SYSTEM_PROMPT = """You are a data scientist writing bank-statement parsers
in plain Python. A statement arrives as FRAMES — extracted layout, plain data:

    {"name": ..., "lines": [{"page": int, "y": int, "items": [[text, x1, x2], ...]}, ...]}

y grows downward; items on a line are in print order. A ledger is a geometry
problem: the header row names the columns, every item's x-center falls under
one of them, and label values (account number, opening/closing balance) sit
on the same line as their label. pandas or plain loops solve it.

Your deliverable — a complete parser module. Nothing else persists:

    KEY = "us__bank__product__1"     # registry key, lowercase
    def parse(frames) -> dict:       # pure: derives everything from frames
        ...
        return {
            "bank_name": str,
            "account_number": str,
            "opening_balance": float,
            "closing_balance": float,
            "transactions": [
                {"date": "YYYY-MM-DD" or None,  # yearless tokens: take the
                 "description": str,             # year from the statement period
                 "amount": float,                # SIGNED: credits +, debits -
                 "balance": float or None},      # printed running balance, if any
            ],
            # OPTIONAL, but bind it when the statement prints section totals
            # ("Total deposits and other additions", "Total withdrawals ...").
            # Each total is an INDEPENDENT accuracy check — kind "positive"
            # matches Σ positive amounts, "negative" matches Σ |debits|, "all"
            # matches the plain sum:
            "totals": [
                {"label": "Total deposits and other additions",
                 "kind": "positive", "value": 1234.56},
            ],
        }

Rules of the artifact:
- parse() may import ONLY the stdlib (re, datetime, json, collections, ...)
  plus pandas/numpy and pyledger. No files, network, or processes; no
  try/except — errors must crash loudly.
- No literals for printed amounts, dates, or transaction descriptions (the
  lint rejects them). Label TERMS like "Opening balance:" are bank
  furniture — literals are fine and expected.
- It must generalize across months of THIS bank: derive columns from each
  statement's own header row and stop terms from the words it prints; never
  hardcode one month's geometry or contents.

Sandbox environment (import and go):
- import pyledger as pl
- THE DOCSTRINGS ARE THE MANUAL: every helper's full contract, examples and
  failure modes are one print away — print(pl.parse_amount.__doc__),
  print(pl.reconcile.__doc__), help(pl).
- pl.load_frames() -> the statements' frames LIST (frames.json in cwd).
  parse() receives ONE element of it: a single statement dict
  {"name": ..., "lines": [...]} — the #1 drafting crash is treating frames
  as something else.
- pl.show(lines, n=15, start=0) — layout slice with line indices
- pl.items(frames) -> [{text, x1, x2, cx, y, page, line}] per printed item —
  pd.DataFrame(pl.items(frames)) and wrangle freely
- pl.parse_amount("$1,234.56 DR") -> signed float (CR +, DR -, parens
  negate); None for unreadable tokens — None is NOT zero, it means "no
  money here" (a description-wrap line)
- pl.parse_date("05 Mar", default_year=2026) -> "2026-03-05"; None if not
  date-shaped
- pl.reconcile(amounts, opening, closing, balances=None) — the ledger check;
  must print OK before you finish

Verification is arithmetic, not opinion: the harness imports your module and
runs parse() on EVERY statement given; each must yield a valid canonical dict
AND reconcile (opening + sum(amounts) == closing; printed balances must track
when present; bound totals must match their parsed sums — they catch what
end-to-end reconcile cannot: a dropped offsetting pair or a double sign
flip still balances). Every gate error is remedial — crashes carry a HINT,
schema errors name the row and the fix, reconcile failures name the
divergent row. Read the error; it describes exactly what to change. End the
script with a demo guarded by if __name__ == "__main__": parse the first
statement, print it, reconcile.
"""

PY_CONTRACT = """Contract for every response:
- ONE complete runnable python script in a single ```python fence: imports,
  KEY, def parse(frames), then the demo guarded by if __name__ == "__main__".
- KEEP IT SHORT: under ~70 lines. No try/except anywhere — an error must
  crash; the harness feeds the exact traceback back to you.
- Literals only for label terms / column header words — never for printed
  amounts, dates, or transaction descriptions.
- BURNER PROBES: to just LOOK before committing, first line exactly
  "# probe" (print anything). Probes are discarded and uncapped, but they
  cannot converge the run; from 90% of turns used onward you are warned.
- MANDATORY LOOKING: your first full script is not accepted until probes
  have shown the TOP and the END of the statement — the words that
  terminate the table are at the end. Read them, never guess.
- Never finish while your own pl.reconcile prints FAIL — the drift names
  what is wrong (a stop term cutting the table early, a dropped row, a
  wrong sign)."""

PY_EXAMPLE = '''import re

import pyledger as pl

KEY = "us__demo__checking__1"


def parse(frames):
    lines = frames["lines"]

    def beside(label):                      # value printed right of a label
        for line in lines:
            for text, x1, _x2 in line["items"]:
                if label in text:
                    right = [t for t, a, _b in line["items"] if a > x1]
                    if right:
                        return " ".join(right)
        raise ValueError(f"label not printed: {label}")

    account = beside("Account No:")
    opening = pl.parse_amount(beside("Beginning Balance"))
    closing = pl.parse_amount(beside("Ending Balance"))
    year = int(re.search(r"\\b(19|20)\\d{2}\\b", beside("Statement Period")).group())

    header = next(line for line in lines                 # the column header row
                  if {"Date", "Amount"} <= {i[0] for i in line["items"]})
    cols = sorted(((t, (x1 + x2) / 2) for t, x1, x2 in header["items"]),
                  key=lambda c: c[1])
    names = [n for n, _ in cols]
    centers = [c for _, c in cols]
    edges = ([float("-inf")] + [(a + b) / 2 for a, b in zip(centers, centers[1:])]
             + [float("inf")])

    def column(cx):                        # which header an item sits under
        for name, low, high in zip(names, edges, edges[1:]):
            if low <= cx < high:
                return name
        return names[-1]

    transactions, pending_desc, pending_date = [], "", None
    for line in lines[lines.index(header) + 1:]:
        if "End of activity" in pl.line_text(line):      # stop term, READ it
            break
        row = {}
        for text, x1, x2 in line["items"]:
            name = column((x1 + x2) / 2)
            row[name] = (row.get(name, "") + " " + text).strip()
        if not any(row.values()):
            continue
        date = pl.parse_date(row.get("Date", ""), default_year=year)
        amount = pl.parse_amount(row.get("Amount", ""), strict_grouping=True)
        if amount is None:                  # no money: description wrap line
            pending_date = date or pending_date
            pending_desc = (pending_desc + " " + row.get("Description", "")).strip()
            continue
        transactions.append({
            "date": date or pending_date,
            "description": (pending_desc + " " + row.get("Description", "")).strip(),
            "amount": amount,
            "balance": pl.parse_amount(row.get("Balance", "")),
        })
        pending_desc, pending_date = "", None

    return {"bank_name": beside("Bank"), "account_number": account,
            "opening_balance": opening, "closing_balance": closing,
            "transactions": transactions}


if __name__ == "__main__":
    statement = parse(pl.load_frames()[0])
    print(statement["account_number"], statement["opening_balance"],
          "->", statement["closing_balance"])
    for txn in statement["transactions"][:5]:
        print(txn)
    pl.reconcile([t["amount"] for t in statement["transactions"]],
                 statement["opening_balance"], statement["closing_balance"],
                 [t["balance"] for t in statement["transactions"]])
'''

PY_TASK_TEMPLATE = """Statement to parse: {name} ({pages} pages, {lines} layout
lines). frames.json in the working directory holds it; pl.load_frames() reads
it.

Explore until the layout is clear (labels, header row, column spans, the
words that end the table, wrapped-description rows), then write the parser:
KEY + def parse(frames) returning the canonical dict, ending with the demo
under if __name__ == "__main__": print the parsed statement and reconcile.
Return the complete script in one ```python fence.

When unsure what a label or stop word says, first return a BURNER PROBE —
first line "# probe", print what you need (pl.show(...) slices). It is
discarded; then return the full script."""

GATE_RUNNER = '''import importlib.util
import json
from pathlib import Path

import pyledger as pl

spec = importlib.util.spec_from_file_location("candidate", Path("{candidate}.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
verdict = pl.evaluate(module.parse, pl.load_frames(),
                      key=getattr(module, "KEY", None))
print("GATE:" + json.dumps(verdict))
'''


def py_gate(workdir: Path, stem: str, trace: Trace) -> dict:
    """The py-mode engine gate: import workdir/<stem>.py in a fresh sandbox
    and run parse() on EVERY statement. Verdict shape matches the config
    engine's evaluate() so the loop's bookkeeping is unchanged. Works for
    round scripts (round_N) and the maintain-mode baseline alike."""
    (workdir / f"gate_{stem}.py").write_text(GATE_RUNNER.format(candidate=stem))
    outcome = run_script(workdir, f"gate_{stem}")
    verdict = None
    if outcome["returncode"] == 0:
        match = re.search(r"^GATE:(\{.*\})$", outcome["stdout"], re.M)
        if match:
            verdict = json.loads(match.group(1))
    if verdict is None:
        error = outcome["stderr"][-400:] or outcome["stdout"][-200:]
        verdict = {"key": None, "stage": "parse", "clean": 0, "verified": False,
                   "results": [{"name": "gate", "ok": False, "rows": 0,
                                "error": f"gate failed: {error}"}]}
    if verdict.get("verified"):
        import pyledger

        if not pyledger.KEY_RE.match(verdict.get("key") or ""):
            verdict["verified"] = False
            verdict["results"].append({
                "name": "key", "ok": False, "rows": 0,
                "error": (f"KEY {verdict.get('key')!r} must be "
                          "cc__bank__product__n, lowercase"),
            })
    trace.log(
        f"{stem} pyledger gate",
        f"exit {outcome['returncode']}\n{json.dumps(verdict, default=str)}",
    )
    return verdict


def py_feedback(evaluation: dict) -> str:
    """Per-statement verdict lines for a py-mode round."""
    lines = []
    for result in evaluation["results"]:
        if result["ok"]:
            lines.append(f"  {result['name']}: OK — {result['rows']} transactions")
        else:
            lines.append(f"  {result['name']}: FAILED — {result['error']}")
    return ("pyledger gate (parse() re-run on EVERY statement; schema + "
            "reconcile required):\n" + "\n".join(lines))


# --- py maintain mode: evolve a registered parser, never from scratch ---------

MAINTAIN_ADDENDUM = """MAINTENANCE MODE. You are revising an EXISTING registered parser that
already works for at least one statement. Rules of maintenance:
- KEY stays exactly as given — never rename, never register a new key.
- Make the SMALLEST change that fixes the failing statement(s) WITHOUT
  breaking the passing one(s). The gate runs parse() on EVERY statement;
  all must verify. Prefer a new fallback branch or a widened condition over
  a restructure; keep what already works working.
- Diagnose from the baseline verdict first: it names each failing statement
  and the exact error (crash hint, schema row, or reconcile divergence).
  Read it before touching anything.
- When a statement prints section totals ("Total deposits and other
  additions", ...), bind them into totals[] — independent accuracy checks
  that catch errors end-to-end reconcile mathematically cannot see.
- You already hold the layout knowledge in the baseline code; probe (first
  line '# probe', as always) only what the baseline doesn't explain — e.g.
  the failing statement's header row, its stop words, its money formats."""

MAINTAIN_TASK_TEMPLATE = """Maintaining the registered parser {key}.
Statement set: {names}. frames.json in the working directory holds them all
(first: {name}, {pages} pages, {lines} layout lines).

Baseline verdict (the CURRENT registry code, run on every statement):
{verdict}

Current registered source:
```python
{code}
```

Tweak it minimally so EVERY statement verifies. Keep KEY = "{key}" exactly.
Return the complete revised script in one ```python fence; probe first when
the baseline doesn't explain a failure."""


# --- LLM transport (raw code, not json_object) --------------------------------


def chat_code(messages: list[dict]) -> str:
    """Raw-code chat completion with retry. Works against the local
    llama-server and OpenAI-compatible clouds (OpenRouter et al): Bearer
    auth from TRANSTRACTOR_LLM_API_KEY / OPENROUTER_API_KEY, and the
    llama-server-only chat_template_kwargs switch is sent just to local
    endpoints — remote providers 400 on unknown body fields. Free-tier
    endpoints answer with in-band error payloads or 429s; those retry with
    backoff instead of killing the run."""
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("TRANSTRACTOR_LLM_API_KEY") or os.environ.get(
        "OPENROUTER_API_KEY"
    )
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body: dict = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
    }
    if re.match(r"https?://(127\.|localhost|\[::1\])", BASE_URL):
        body["chat_template_kwargs"] = {"enable_thinking": False}
    # OpenRouter routing controls (no-ops elsewhere, absent unless set):
    # TRANSTRACTOR_LLM_PROVIDER pins endpoint(s), comma-separated, e.g.
    # "amazon-bedrock/us-east-1" -> provider.only + no fallbacks (dodges
    # guardrail-excluded endpoints); TRANSTRACTOR_LLM_REASONING=true enables
    # reasoning for models that want it.
    provider_pin = os.environ.get("TRANSTRACTOR_LLM_PROVIDER", "").strip()
    if provider_pin:
        body["provider"] = {
            "only": [p.strip() for p in provider_pin.split(",") if p.strip()],
            "allow_fallbacks": False,
        }
    if os.environ.get("TRANSTRACTOR_LLM_REASONING", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        body["reasoning"] = {"enabled": True}

    delays = (0, 5, 15, 45)
    last_error = "unknown"
    # Accept both conventions: the API root (…/v1, to which /chat/completions
    # is appended) or the full endpoint path as written in .env.
    endpoint = BASE_URL.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode(),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                payload = json.load(response)
            content = payload.get("choices", [{}])[0].get("message", {}).get("content")
            if content and content.strip():
                return content
            last_error = f"empty/absent content: {json.dumps(payload)[:300]}"
        except urllib.error.HTTPError as error:
            last_error = f"HTTP {error.code}: {error.read()[:300]!r}"
            if error.code not in (429, 402) and error.code < 500:
                hint = (
                    " (context window overflow is the usual cause)"
                    if error.code in (400, 413)
                    else " (guardrail/data-policy endpoint exclusions show up as 404)"
                    if error.code == 404
                    else ""
                )
                raise RuntimeError(  # 4xx is not transient — do not retry
                    f"chat endpoint rejected the request: {last_error}{hint}"
                ) from error
        except urllib.error.URLError as error:
            last_error = f"transport: {error}"
        print(f"  chat attempt {attempt + 1} failed ({last_error[:120]}); retrying")
    raise RuntimeError(f"chat endpoint failed after {len(delays)} attempts: {last_error}")


def extract_code(text: str) -> str | None:
    """The python code block in a response (tolerant: any fence, or a bare
    script that starts with an import or the # probe marker)."""
    match = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    if match:
        return match.group(1).strip()
    if text.lstrip().startswith(("# probe", "import ", "from ", "#!")):
        return text.strip()
    return None


# --- Transcription guard -------------------------------------------------------

MONEY_LITERAL_RE = re.compile(r"^\$?\d{1,3}(,\d{3})+(\.\d{2})?\s*(CR|DR)?$", re.I)


def lint_code(code: str) -> str | None:
    """Reject scripts that smuggle statement data in as literals, and broad
    except handlers that would swallow the errors the feedback loop depends
    on. Regex strings pass; range(...) bounds are exempt. Returns the
    violation text or None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        return f"script does not parse: {error}"
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and (
            node.type is None
            or (isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"))
        ):
            return (
                "try/except would swallow the error and hide it from you — "
                "let the script crash; the harness feeds the exact traceback "
                "back"
            )
    range_args = {
        arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "range"
        for arg in node.args
        if isinstance(arg, ast.Constant)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
            continue
        value = node.value
        if isinstance(value, str) and MONEY_LITERAL_RE.match(value.strip()):
            return (
                f"string literal {value!r} looks like a printed amount — "
                "transcribing statement values into the script is forbidden; "
                "derive values from the frames at runtime"
            )
        if (
            isinstance(value, (int, float))
            and abs(value) > 9999
            and node not in range_args
        ):
            return (
                f"numeric literal {value} is in the transaction-amount range — "
                "hardcoding data is forbidden; compute values from the frames"
            )
    return None


# --- Sandboxed execution -------------------------------------------------------


def _child_limits() -> None:
    """Best-effort rlimits for the sandboxed child. Each is applied
    independently and skipped where the platform refuses it — macOS rejects
    RLIMIT_AS outright and low NOFILE ceilings below its default soft limit.
    An exception in preexec_fn would kill subprocess.run with a detail-free
    SubprocessError, so nothing here may raise."""
    import resource

    for limit, value in (
        (resource.RLIMIT_CPU, (30, 30)),
        (resource.RLIMIT_FSIZE, (8 << 20, 8 << 20)),
        (resource.RLIMIT_AS, (1536 << 20, 1536 << 20)),  # pandas headroom
        (resource.RLIMIT_NOFILE, (64, 64)),
    ):
        try:
            resource.setrlimit(limit, value)
        except (ValueError, OSError):
            pass


def network_prefix() -> list[str]:
    """['unshare', '-rn', '--'] when unprivileged user namespaces can give
    the child a network-less namespace; [] (plain run) otherwise."""
    if not shutil.which("unshare"):
        return []
    try:
        probe = subprocess.run(
            ["unshare", "-rn", "--", "/bin/true"],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return ["unshare", "-rn", "--"] if probe.returncode == 0 else []


def run_script(workdir: Path, name: str) -> dict:
    """Run workdir/name.py in the sandbox; returns what the model sees."""
    script = workdir / f"{name}.py"
    command = [
        *network_prefix(),
        sys.executable,
        # -E -s: ignore PYTHON* env vars and user site-packages (the child
        # env is already sanitised); unlike -I this KEEPS the script's own
        # directory on sys.path so `import distill_emulator` resolves.
        "-E",
        "-s",
        script.name,
    ]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=workdir,
            env={
                "HOME": str(workdir),
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C.UTF-8",
                "PYTHONIOENCODING": "utf-8",
            },
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT,
            preexec_fn=_child_limits,
        )
        outcome = {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "elapsed": time.perf_counter() - started,
        }
    except subprocess.TimeoutExpired:
        outcome = {
            "returncode": -1,
            "stdout": "",
            "stderr": f"script exceeded the {SCRIPT_TIMEOUT}s wall-clock limit",
            "elapsed": SCRIPT_TIMEOUT,
        }
    (workdir / f"{name}.out").write_text(
        f"returncode: {outcome['returncode']}\n--- stdout ---\n"
        f"{outcome['stdout']}\n--- stderr ---\n{outcome['stderr']}\n"
    )
    return outcome


# --- Frames extraction ---------------------------------------------------------


def frames_for(pdfs: list[Path], workdir: Path) -> tuple[dict, str]:
    """Write frames.json; returns (frames dict, layout text of statement 0)."""
    from transtractor import Parser

    parser = Parser(rules_only=True)
    statements = []
    layout_text = ""
    for pdf in pdfs:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            layout_path = tmp.name
        try:
            parser.layout(str(pdf), layout_path)
            text = Path(layout_path).read_text()
        finally:
            Path(layout_path).unlink(missing_ok=True)
        if pdf == pdfs[0]:
            layout_text = text
        page = 0
        lines = []
        for raw in text.splitlines():
            header = re.match(r"^\[Page (\d+)\]$", raw.strip())
            if header:
                page = int(header.group(1))
                continue
            items = LAYOUT_ITEM_XY_RE.findall(raw)
            if not items:
                continue
            lines.append(
                {
                    "page": page,
                    "y": int(items[0][3]),
                    "items": [[text_, int(x1), int(x2)] for text_, x1, x2, *_ in items],
                }
            )
        statements.append({"name": pdf.name, "lines": lines})
    frames = {"statements": statements}
    (workdir / "frames.json").write_text(json.dumps(frames))
    return frames, layout_text


# --- Trace ---------------------------------------------------------------------


class Trace:
    """Append-only run log next to the scripts and frames it describes."""

    def __init__(self, directory: Path):
        self.path = directory / "trace.log"
        self._file = self.path.open("a", encoding="utf-8")

    def log(self, section: str, body: str = "") -> None:
        self._file.write(
            f"\n{'=' * 78}\n[{time.strftime('%H:%M:%S')}] {section}\n"
            f"{'-' * 78}\n{body}\n"
        )
        self._file.flush()

    def line(self, text: str) -> None:
        self._file.write(f"{text}\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


# --- The loop ------------------------------------------------------------------


def compact_messages(messages: list[dict]) -> int:
    """Elide superseded conversation bulk so a 30-turn loop fits the context.

    Full scripts are stateless by design — each round re-emits the whole
    script — so an old script's value is near zero while being both the
    biggest token mass and the parrot bait the model copies instead of
    thinking. Keep the newest 2 full scripts verbatim; older ones become a
    placeholder. Probe outputs older than the last 3 are truncated (the
    observations stay, in abbreviated form). Returns chars saved."""
    before = sum(len(str(m.get("content", ""))) for m in messages)
    full_script_indexes = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "assistant"
        and isinstance(m.get("content"), str)
        and not m["content"].lstrip().startswith("# probe")
        and len(m["content"]) > 500
    ]
    for i in full_script_indexes[:-2]:
        messages[i]["content"] = (
            "(an earlier full script was here; omitted to save context — "
            "write the current script fresh, guided by the newest feedback)"
        )
    probe_feedback_indexes = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and "End of probe" in m["content"]
        and len(m["content"]) > 700
    ]
    for i in probe_feedback_indexes[:-3]:
        messages[i]["content"] = messages[i]["content"][:700] + "\n(older probe output truncated)"
    return before - sum(len(str(m.get("content", ""))) for m in messages)


def script_feedback(outcome: dict) -> str:
    stdout = outcome["stdout"][-OUTPUT_TAIL:]
    stderr = outcome["stderr"][-1500:]
    parts = []
    if outcome["returncode"] != 0:
        parts.append(
            "The script CRASHED — fix exactly this error and keep everything "
            f"else identical:\n{stderr or '(no stderr)'}"
        )
    if stdout.strip():
        parts.append(f"Your script printed:\n{stdout}")
    elif outcome["returncode"] == 0:
        parts.append(
            "The script printed nothing — print() your probes; that output "
            "is your only view of the statement."
        )
    return "\n\n".join(parts)


def draft_by_repl(
    pdfs: list[Path],
    workdir: Path,
    rounds: int,
    trace: Trace,
    canned_script: Path | None = None,
    system_prompt: str = SYSTEM_PROMPT,
    task_template: str = TASK_TEMPLATE,
    engine: str = "config",
    task_message: str | None = None,
    require_key: str | None = None,
    enforce_look_gate: bool = True,
    max_probes: int | None = None,
) -> tuple[dict | None, bool, list[dict]]:
    """Returns (best artifact or None, verified?, round history). The artifact
    is a config dict (engine='config') or {'key', 'code'} for a pyledger
    parser module (engine='py'). Maintain mode (require_key set) pins KEY to
    the registered parser being evolved; task_message replaces the drafted
    task template (it embeds the baseline source and its gate verdict)."""
    if engine == "py":
        import pyledger

        frames, layout_text = pyledger.frames_for(pdfs, workdir)
    else:
        frames, layout_text = frames_for(pdfs, workdir)
    first = frames["statements"][0]
    trace.log(
        "frames",
        f"{len(frames['statements'])} statement(s); {first['name']}: "
        f"{len(first['lines'])} lines",
    )

    messages: list[dict] = []
    history: list[dict] = []
    best: tuple[int, dict] | None = None
    previous_code: str | None = None
    consecutive_probes = 0
    probes_done = 0
    tail_seen = False
    total_lines = len(first["lines"])
    chars_in = 0
    chars_out = 0

    for round_number in range(rounds):
        started = time.perf_counter()
        if canned_script is not None:
            code = canned_script.read_text()
        else:
            task = task_message or task_template.format(
                name=first["name"],
                pages=1 + max(line["page"] for line in first["lines"]),
                lines=len(first["lines"]),
            )
            if not messages:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": task},
                ]
            saved = compact_messages(messages)
            if saved > 0:
                trace.log(f"round {round_number} context compacted", f"{saved} chars elided")
            try:
                raw = chat_code(messages)
            except RuntimeError as error:
                trace.log(f"round {round_number} chat failed", str(error))
                print(f"round {round_number}: chat failed, skipping ({str(error)[:90]})")
                history.append(
                    {"round": round_number, "outcome": f"chat failed: {str(error)[:120]}"}
                )
                continue
            chars_out += len(raw)
            code = extract_code(raw)
            trace.log(f"round {round_number} raw model response", raw)
            if code is None:
                feedback = (
                    "Your response contained no ```python code block. Return "
                    "ONE complete script in a single fence."
                )
                messages.append({"role": "assistant", "content": raw[:2000]})
                messages.append({"role": "user", "content": feedback})
                trace.log(f"round {round_number} feedback sent", feedback)
                history.append({"round": round_number, "outcome": "no code block"})
                print(f"round {round_number}: no code block in response")
                continue
            messages.append({"role": "assistant", "content": code})

        violation = lint_code(code)
        if violation:
            trace.log(f"round {round_number} lint rejected", violation)
            print(f"round {round_number}: lint rejected ({violation[:70]})")
            if canned_script is not None:
                history.append({"round": round_number, "outcome": f"lint: {violation}"})
                return None, False, history
            feedback = (
                f"REJECTED before running: {violation}\nReturn the complete "
                "corrected script."
            )
            messages.append({"role": "user", "content": feedback})
            history.append({"round": round_number, "outcome": f"lint: {violation}"})
            continue

        # Burner probe: look-before-you-conclude turns. Discarded by design —
        # nothing is distilled, nothing is evaluated, and convergence still
        # requires one complete script through the full engine gate.
        is_probe = code.lstrip().splitlines()[0].strip().lower() == "# probe"
        (workdir / f"round_{round_number}.py").write_text(code)

        # Commit latch: once 90% of the turn budget is spent, probes close
        # for good — the run must converge on full scripts. The look-gate is
        # waived from the same point so a model that never probed cannot
        # deadlock between "probe first" and "probes are closed".
        commit_mode = round_number >= int(rounds * 0.9)

        # Hand-holding gate: the first full script is only accepted after the
        # model has actually LOOKED — at least one probe, and at least one
        # probe that showed the END of the statement (detectable: show()
        # prints line indices, so a high index in probe stdout means the tail
        # was on screen). Small models guess stop words unless forced to read
        # them; this is the forcing. Waived once the probe budget is spent —
        # otherwise bounces and the gate deadlock (the model can neither
        # probe again nor commit).
        budget_spent = (max_probes is not None and probes_done >= max_probes)
        if (
            not is_probe
            and not commit_mode
            and not budget_spent
            and canned_script is None
            and enforce_look_gate
            and (probes_done < 1 or not tail_seen)
        ):
            if probes_done < 1:
                instruction = (
                    "Return a burner probe (first line exactly '# probe') "
                    "printing the TOP of the statement: "
                    + ("pl.show(pl.load_frames()[0]['lines'], 12)."
                       if engine == "py" else "emu.show(lines, 12).")
                )
                reason = "no probe yet"
            else:
                show_tail = (
                    "pl.show(lines, 6, len(lines) - 6)" if engine == "py"
                    else "emu.show(lines, 6, len(lines) - 6)"
                )
                instruction = (
                    "Your probes have not shown the END of the statement — "
                    "the words that terminate the transaction table live "
                    "there. Return a burner probe (first line '# probe'): "
                    f"{show_tail}, and READ what "
                    "follows the last transaction row."
                )
                reason = "tail not probed"
            feedback = (
                "NOT ACCEPTED — look before you write. Small models that "
                "guess labels and stop words instead of reading them fail; "
                f"{instruction}"
            )
            trace.log(f"round {round_number} full script gated", f"{reason}\n" + feedback)
            print(f"round {round_number}: full script gated ({reason})")
            messages.append({"role": "user", "content": feedback})
            history.append(
                {"round": round_number, "outcome": f"full script gated: {reason}"}
            )
            continue
        if is_probe and "build_config(" in code and engine != "py":
            feedback = (
                "A probe must NOT call emu.build_config — probes are burner "
                "turns. Remove the '# probe' first line to submit the full "
                "script."
            )
            messages.append({"role": "user", "content": feedback})
            history.append({"round": round_number, "outcome": "probe+config rejected"})
            continue
        # Probe budget: small models ignore the 90% warning and probe forever
        # (the local 4B spent 29 of 30 turns probing, one full script, zero
        # gate feedback). With a budget set, probes beyond it are BOUNCED —
        # not executed — with commit-now feedback.
        if (is_probe and max_probes is not None and probes_done >= max_probes
                and canned_script is None):
            feedback = (
                f"PROBE BUDGET ({max_probes}) EXHAUSTED — you have seen "
                "enough. Return the COMPLETE full script now: KEY + def "
                "parse(frames) + the demo guarded by if __name__ == "
                "'__main__'. It does not need to be perfect; the gate runs "
                "it, and the error names exactly what to fix next round."
            )
            messages.append({"role": "user", "content": feedback})
            history.append(
                {"round": round_number, "outcome": f"probe bounced (budget {max_probes})"}
            )
            trace.log(f"round {round_number} probe bounced", "budget exhausted")
            continue
        if is_probe:
            consecutive_probes += 1
            probes_done += 1
            outcome = run_script(workdir, f"round_{round_number}")
            # Tail coverage: show() prints zero-padded line indices at the
            # start of each row; an index near the document end means the
            # stop-word region was actually displayed.
            shown = [
                int(index)
                for index in re.findall(
                    r"^(\d{1,4}) [\[{]", outcome["stdout"], re.M)
            ]
            if shown and max(shown) >= total_lines - 8:
                tail_seen = True
            trace.log(
                f"round {round_number} probe output",
                f"exit {outcome['returncode']}\n{outcome['stdout'][-4000:]}"
                f"\n--- stderr ---\n{outcome['stderr'][-2000:]}",
            )
            verdict = "probe ok" if outcome["returncode"] == 0 else "probe crashed"
            print(f"round {round_number}: {verdict} (burner, #{consecutive_probes})")
            history.append({"round": round_number, "outcome": f"{verdict} (burner)"})
            parts = [script_feedback(outcome)]
            warning = (
                "WARNING: 90% of the turns are used — probes cannot converge "
                "the run. Return the COMPLETE full script next (keep needed "
                "prints inside it, verify with emu.reconcile, "
                "emu.build_config last)."
                if commit_mode
                else "Probe again, or return the complete full script."
            )
            parts.append(
                "End of probe (discarded — nothing was distilled). " + warning
            )
            feedback = "\n\n".join(parts)
            trace.log(f"round {round_number} probe feedback sent", feedback)
            messages.append({"role": "user", "content": feedback})
            continue
        consecutive_probes = 0

        if engine == "py" and require_key:
            declared = re.search(r'^KEY\s*=\s*["\']([^"\']+)["\']', code, re.M)
            if not declared or declared.group(1) != require_key:
                feedback = (
                    f"MAINTAIN MODE: KEY must stay {require_key!r}"
                    + (f", but the script declares {declared.group(1)!r}"
                       if declared else ", but the script declares none")
                    + " — you are evolving the registered parser, not "
                    "registering a new one. Return the corrected script."
                )
                messages.append({"role": "user", "content": feedback})
                history.append({"round": round_number, "outcome": "key renamed"})
                trace.log(f"round {round_number} rejected", feedback)
                continue

        if engine == "py":
            outcome = run_script(workdir, f"round_{round_number}")
            trace.log(
                f"round {round_number} script output",
                f"exit {outcome['returncode']} in {outcome['elapsed']:.1f}s\n"
                f"{outcome['stdout'][-4000:]}\n--- stderr ---\n{outcome['stderr'][-2000:]}",
            )
            config = None
            if outcome["returncode"] == 0 and "def parse(" in code:
                evaluation = py_gate(workdir, f"round_{round_number}", trace)
                if evaluation.get("clean", 0) > 0:
                    config = {"key": evaluation.get("key"), "code": code}
            else:
                evaluation = None
        else:
            (workdir / "config.json").unlink(missing_ok=True)
            outcome = run_script(workdir, f"round_{round_number}")
            trace.log(
                f"round {round_number} script output",
                f"exit {outcome['returncode']} in {outcome['elapsed']:.1f}s\n"
                f"{outcome['stdout'][-4000:]}\n--- stderr ---\n{outcome['stderr'][-2000:]}",
            )

            config: dict | None = None
            if (workdir / "config.json").exists():
                config = json.loads((workdir / "config.json").read_text())
                applied = derive_alignments(config, layout_text)
                try:
                    evaluation = evaluate(config, pdfs)
                except BaseException as panic:  # noqa: BLE001 - pyo3 PanicException
                    # Engine panics derive BaseException, not Exception —
                    # contain them here so a pathological draft becomes round
                    # feedback instead of killing the run.
                    evaluation = {
                        "stage": "parse",
                        "results": [
                            {
                                "pdf": pdfs[0].name,
                                "ok": False,
                                "error": f"ENGINE PANIC: {panic}",
                                "rows": 0,
                                "first_error": f"{panic}"[:200],
                            }
                        ],
                        "clean": 0,
                        "verified": False,
                    }
                if applied:
                    trace.log(
                        f"round {round_number} auto-derived alignments",
                        ", ".join(applied),
                    )
                trace.log(
                    f"round {round_number} engine evaluation",
                    json.dumps(evaluation, indent=2, default=str)[:6000],
                )
            else:
                evaluation = None

        elapsed = time.perf_counter() - started
        if evaluation is not None and evaluation.get("verified"):
            summary = "VERIFIED"
        elif evaluation is not None:
            summary = (
                f"{evaluation.get('clean', 0)}/{len(pdfs)} clean"
                if evaluation["stage"] == "parse"
                else f"rejected at validation: {evaluation['error'][:90]}"
            )
        else:
            if engine == "py":
                summary = ("script crashed" if outcome["returncode"] != 0
                           else "no parse() exercised")
            else:
                summary = "no config distilled"

        print(f"round {round_number}: {summary} ({elapsed:.1f}s)")
        history.append({"round": round_number, "outcome": summary})
        if trace:
            trace.line(f"round {round_number}: {summary} ({elapsed:.1f}s)")

        if evaluation is not None and evaluation.get("verified"):
            return config, True, history
        if evaluation is not None and evaluation["stage"] == "parse" and evaluation["clean"] > 0:
            if best is None or evaluation["clean"] > best[0]:
                best = (evaluation["clean"], config)

        if canned_script is not None:
            return (best[1] if best else config), False, history

        # Assemble the REPL feedback: the script's own reconcile verdict is
        # the loudest signal (a FAIL it ignored explains most engine
        # failures), then the engine verdict, then what it printed.
        parts = []
        if "reconcile: FAIL" in outcome["stdout"]:
            parts.append(
                "YOUR OWN reconcile printed FAIL — the drift names what is "
                "wrong (a stop term cutting the table early, a dropped row, "
                "a wrong sign, a mis-binned column). Fix parse() until it "
                "prints OK."
                if engine == "py" else
                "YOUR OWN reconcile FAILED and you called build_config anyway "
                "— forbidden. The reconcile drift tells you what is wrong: "
                "a stop term cutting the table early (too few rows), a "
                "dropped column, or a wrong sign. Fix the region/roles and "
                "make reconcile print OK BEFORE building the config."
            )
        if engine == "py":
            if evaluation is not None:
                parts.append(py_feedback(evaluation))
            elif outcome["returncode"] != 0:
                import pyledger

                last = (outcome["stderr"].strip().splitlines() or [""])[-1]
                hint = pyledger.hint_for(last)
                if hint:
                    parts.append(f"HINT: {hint}")
            elif "def parse(" not in code:
                parts.append(
                    "The script never defines def parse(frames) — the "
                    "deliverable is the parser module: KEY, parse(), demo "
                    "under if __name__ == '__main__'."
                )
        elif evaluation is not None:
            engine_feedback = build_feedback(evaluation, pdfs, config)
            engine_feedback = engine_feedback.replace(
                "return the full corrected config",
                "fix these fields in your emu.build_config call",
            ).replace(
                "Return the complete corrected configuration JSON only.",
                "Return the complete corrected script.",
            )
            parts.append(engine_feedback)
        else:
            parts.append(
                "No config.json was produced — the script must call "
                "emu.build_config(**fields) as its final step."
            )
        parts.append(script_feedback(outcome))
        feedback = "\n\n".join(parts) + "\n\nReturn the complete corrected script."
        chars_in += len(feedback)
        trace.log(f"round {round_number} feedback sent", feedback)

        if code == previous_code:
            trace.line(
                f"round {round_number}: escalation — context reset (model "
                "parroted its previous script)"
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Your previous script failed. The diagnosis:\n"
                        f"{feedback[-3000:]}\n\nRewrite the complete script "
                        "fixing exactly what the diagnosis names. Probes "
                        "first, "
                        + ("KEY + def parse last." if engine == "py"
                           else "emu.build_config last.")
                    ),
                },
            ]
        else:
            messages.append({"role": "user", "content": feedback})
        previous_code = code

    trace.log(
        "token accounting (chars)",
        f"responses: {chars_out}, feedback sent: {chars_in}",
    )
    return (best[1] if best else None), False, history


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("pdfs", nargs="+", type=Path, help="statements from one bank")
    cli.add_argument(
        "--rounds",
        type=int,
        default=30,
        help="REPL turns (each is a full script + engine gate; default 30)",
    )
    cli.add_argument(
        "--script",
        type=Path,
        default=None,
        help="run this probe script instead of asking the model (canned test)",
    )
    cli.add_argument("--no-save", action="store_true", help="never write the registry")
    cli.add_argument(
        "--lean",
        action="store_true",
        help="pandas/numpy exploration mode: frames as a DataFrame via "
        "lean_emulator (le), free-form wrangling, same engine gate",
    )
    cli.add_argument(
        "--py",
        action="store_true",
        help="pyledger engine: the artifact is a Python parser module "
        "(KEY + def parse(frames)), verified by reconciliation on every "
        "statement",
    )
    cli.add_argument(
        "--key",
        default=None,
        help="py mode only: MAINTAIN the registered parser with this key — "
        "start from the registry source, not from scratch; the gate requires "
        "ALL given statements to verify (pass old AND new ones: the old are "
        "the regression set)",
    )
    cli.add_argument(
        "--max-probes",
        type=int,
        default=None,
        help="burner-probe budget; probes beyond it are bounced with "
        "commit-now feedback (small models ignore the 90%% warning and "
        "probe forever; default: unlimited)",
    )
    args = cli.parse_args()
    if args.key and not args.py:
        cli.error("--key (maintain mode) requires --py")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    workdir = Path("logs") / f"distill_{stamp}_{args.pdfs[0].stem[:30]}"
    workdir.mkdir(parents=True, exist_ok=True)
    if args.py:
        shutil.copy(Path(__file__).parent / "pyledger.py", workdir / "pyledger.py")
    else:
        shutil.copy(Path(__file__).parent / "distill_emulator.py", workdir / "distill_emulator.py")
        shutil.copy(Path(__file__).parent / "lean_emulator.py", workdir / "lean_emulator.py")
    trace = Trace(workdir)

    mode = f"canned script {args.script}" if args.script else f"{MODEL} at {BASE_URL}"
    print(f"distill harness: {len(args.pdfs)} statement(s), {mode}")
    print(f"workdir: {workdir}")
    trace.log("run start", f"mode={mode}\npdfs={[str(p) for p in args.pdfs]}")

    if args.py:
        system = (PY_SYSTEM_PROMPT + "\n\n" + PY_CONTRACT
                  + "\n\nA complete parser for a different (demo) bank — "
                    "imitate its shape and length, not its labels:\n\n"
                    "```python\n" + PY_EXAMPLE + "\n```")
        template = PY_TASK_TEMPLATE
    elif args.lean:
        system, template = LEAN_SYSTEM_PROMPT, LEAN_TASK_TEMPLATE
    else:
        system, template = SYSTEM_PROMPT, TASK_TEMPLATE

    # Maintain mode: stage the registered parser as the baseline, gate it
    # against the full statement set (old statements = regression set), and
    # hand the model the source + per-statement verdict as its starting point.
    task_message = None
    require_key = None
    look_gate = True
    baseline = None
    if args.py and args.key:
        import pyledger

        source_path = pyledger.PYREGISTRY / f"{args.key}.py"
        if not source_path.exists():
            print(f"no registered parser {args.key!r} in {pyledger.PYREGISTRY}")
            return 1
        baseline = source_path.read_text()
        (workdir / "baseline.py").write_text(baseline)
        frames, _ = pyledger.frames_for(args.pdfs, workdir)
        verdict = py_gate(workdir, "baseline", trace)
        print(
            "baseline: " + ("VERIFIED" if verdict["verified"] else
                            f"{verdict['clean']}/{len(args.pdfs)} statements reconcile")
        )
        if verdict["verified"]:
            trace.log("result", "baseline already verifies every statement")
            print(f"{args.key} already verifies all {len(args.pdfs)} "
                  "statement(s) — nothing to maintain.")
            trace.close()
            return 0
        first = frames["statements"][0]
        task_message = MAINTAIN_TASK_TEMPLATE.format(
            key=args.key,
            names=", ".join(p.name for p in args.pdfs),
            name=first["name"],
            pages=1 + max(line["page"] for line in first["lines"]),
            lines=len(first["lines"]),
            verdict=py_feedback(verdict),
            code=baseline,
        )
        system += "\n\n" + MAINTAIN_ADDENDUM
        require_key = args.key
        look_gate = False  # the baseline IS the looked-at evidence; probe at will

    config, verified, history = draft_by_repl(
        args.pdfs, workdir, args.rounds, trace, canned_script=args.script,
        system_prompt=system, task_template=template,
        engine="py" if args.py else "config",
        task_message=task_message, require_key=require_key,
        enforce_look_gate=look_gate, max_probes=args.max_probes,
    )
    if config is None or not verified:
        trace.log("result", f"FAILED ({'no artifact' if config is None else 'not verified'})")
        trace.log("round history", json.dumps(history, indent=2))
        trace.close()
        print("\nFAILED: no verified configuration.")
        print(json.dumps(history, indent=2))
        return 1

    if args.py:
        key = config["key"]
        trace.log("verified parser", config["code"])
        if baseline is not None:
            import difflib

            diff = [
                line for line in difflib.unified_diff(
                    baseline.splitlines(), config["code"].splitlines(),
                    lineterm="", n=0)
                if line[:1] in "+-" and line[:3] not in ("+++", "---")
            ]
            print(f"maintained in place: {len(diff)} lines changed "
                  "(git history is the parser's changelog)")
            trace.log("maintenance diff", "\n".join(diff[:80]))
        if args.no_save:
            print(f"\nVERIFIED (not saved, --no-save); parser drafted in {workdir}")
        else:
            pyregistry = Path(__file__).parent / "pyregistry"
            pyregistry.mkdir(exist_ok=True)
            out_path = pyregistry / f"{key}.py"
            out_path.write_text(config["code"])
            print(f"\nVERIFIED parser saved: {out_path}")
            print("Reuse with zero LLM:")
            print(f"  uv run python pyledger.py statement.pdf --key {key}")
            trace.log("result", f"VERIFIED parser saved: {out_path}")
        import pyledger

        for pdf in args.pdfs:
            statement = pyledger.parse(str(pdf), key=key)
            line = (
                f"  {pdf.name}: {len(statement['transactions'])} transactions, "
                f"opening {statement['opening_balance']:,.2f} -> "
                f"closing {statement['closing_balance']:,.2f}"
            )
            print(line)
            trace.line(line)
        trace.close()
        return 0

    trace.log("verified config", json.dumps(config, indent=2))
    if args.no_save:
        print(f"\nVERIFIED (not saved, --no-save); config in {workdir / 'config.json'}")
    else:
        REGISTRY.mkdir(exist_ok=True)
        out_path = REGISTRY / f"{config.get('key', 'drafted')}.json"
        out_path.write_text(json.dumps(config, indent=2))
        print(f"\nVERIFIED config saved: {out_path}")
        print("Reuse with zero LLM:")
        print(f"  parser = Parser(); parser.load({str(out_path)!r}); parser.parse('statement.pdf')")
        trace.log("result", f"VERIFIED config saved: {out_path}")

    from transtractor import Parser

    parser = Parser(rules_only=True)
    if not args.no_save:
        parser.load(str(REGISTRY / f"{config.get('key', 'drafted')}.json"))
    else:
        parser.load_str(json.dumps(config))
    for pdf in args.pdfs:
        sd = parser.parse(str(pdf))
        line = (
            f"  {pdf.name}: {len(sd.transactions)} transactions, "
            f"opening {sd.opening_balance:,.2f} -> closing {sd.closing_balance:,.2f}"
        )
        print(line)
        trace.line(line)
    trace.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
