"""LLM config-drafting harness for transtractor.

The LLM never transcribes transactions. It drafts (and iteratively repairs) a
bank-layout CONFIGURATION; the deterministic rules engine does the
transcription; the balance checkers verify. A verified config is saved to the
local registry and parses every future statement from that bank in
milliseconds with no LLM at all.

Loop per round:
    draft/revise config (LLM, small JSON)
      -> validate + register (Rust, ~40 structural checks)
      -> parse every provided statement (Rust rules engine)
      -> all clean? save to registry : build feedback and revise

Usage (from balance-analysis/):
    uv run python config_harness.py statement1.pdf [statement2.pdf ...]
    uv run python config_harness.py --rounds 6 stmt_jan.pdf stmt_feb.pdf
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from envloader import load_env
from transtractor import ConfigLoadError, ParseError, Parser

# .env in the project root is the default configuration (exports override it).
load_env()

# Endpoint is LAN-portable: point at the machine running llama-server with
# TRANSTRACTOR_LLM_URL=http://<server-ip>:30000/v1 (same variable the fork's
# Parser() already reads).
BASE_URL = os.environ.get("TRANSTRACTOR_LLM_URL", "http://127.0.0.1:30000/v1")
MODEL = os.environ.get("TRANSTRACTOR_LLM_MODEL", "qwen3.5-4b")
TIMEOUT = int(os.environ.get("TRANSTRACTOR_LLM_TIMEOUT", "600"))
ROUNDS = int(os.environ.get("TRANSTRACTOR_LLM_ROUNDS", "5"))
REGISTRY = Path(os.environ.get("TRANSTRACTOR_LLM_CONFIG_DIR", "configs"))

# --- Static prompt material -------------------------------------------------

FORMAT_CATALOG = """Amount/Balance formats (all yield signed decimals):
- format1: "1,234.56" -> 1234.56 | "-1,234.56" -> -1234.56 | "1,234.56-" -> -1234.56
- format2: "$1,234.56" -> 1234.56 | "-$1,234.56" -> -1234.56
- format3: "$1,234.56 CR" -> 1234.56 | "$1,234.56 DR" -> -1234.56
- format4: "1,234.56 CR" -> 1234.56 | "1,234.56 DR" -> -1234.56
- format5: "nil" -> 0.00
- format6: "- $1,234" -> -1234.00 | "+ $1,234" -> 1234.00
- format7: "$1,234.56<any suffix>" -> 1234.56
IMPORTANT: format1-4 require comma grouping for amounts of 1,000 and above
("25,000.00" parses; "25000.00" does not). Match the format to EXACTLY how the
statement prints amounts, including any $, CR/DR suffixes: "$50,000.00 CR" is
format3, "90,350 CR" is format4. Check the layout dump for suffixes.
Date formats (all yield YYYY-MM-DD):
- format1: "24 Mar" / "24 March" (year inferred)
- format2: "24 Mar 2025" | "1 Jan 2025"
- format3: "Mar 24, 2025"
- format4: "24/3/2020"  - format5: "24/3/25"  - format6: "3/24"
- format7: "24-03-2023" | "24-3-23"   - format8: "03-24-2023" | "3-24-23"
- format9: "03/24/2023" | "3/24/23"   - format10: "Mar 24"
- format11: "Mar 24, 2023-Apr 24, 2023"  - format12: "2023/03/24"
- format13: "2023-03-24"  - format14: "03/24/2023*"  - format15: "Jul 1 - Jul 31, 2026\""""

FIELD_GUIDE = """Config fields (JSON; ONLY use fields from this guide — unknown fields and
invalid values are rejected by validation):
- "key": "<country>__<bank>__<product>__<version>" — all lowercase, underscores
  only, no whitespace; the FIRST component MUST be a real two-letter ISO 3166
  country code such as "au", "us" or "gb", e.g. "au__demo__savings__1"
- "account_type": one of "Checking", "Savings", "Credit Card", "Investment",
  "Loan", "Mortgage", "Mixed", "Other"
- "bank_name": human-readable bank name
- "account_terms": [strings that ALL appear somewhere in the statement; used to
  detect this bank. Pick distinctive header words.]
- "account_number_terms": [label before the account number], plus
  "account_number_patterns": [regex matching the number], "account_number_alignment": "y1"
- "opening_balance_terms" / "closing_balance_terms": [label], "opening_balance_formats":
  [amount format], alignment "y1" = value on the SAME LINE as the label.
- "start_date_terms": [label], "start_date_formats": [date format], alignment "y1".
- "transaction_terms": [words marking the START of the transaction table],
  "transaction_terms_stop": [words marking the END of the table].
- "transaction_formats": [column orders]. The ONLY valid column names are
  "date", "description", "amount", "balance" — never "debit"/"credit". A statement
  with separate Debit and Credit columns is modelled as ONE "amount" column:
  "transaction_amount_headers": ["Credit"] picks the credit column as the amount,
  and "transaction_amount_invert_headers": ["Debit"] makes values in the debit
  column negative.
- Per column: "transaction_date_formats", "transaction_description_headers",
  "transaction_amount_formats", "transaction_balance_formats", and
  "transaction_date_headers"/"transaction_amount_headers"/"transaction_balance_headers"
  (column header words). Optional "transaction_amount_invert_headers": [debit column
  header] flips the sign for that column (e.g. separate Credit/Debit columns).
- "transaction_formats": include shortened orders for continuation rows too —
  long tables wrap, so rows may lack the date (["description","amount","balance"])
  or the balance (["date","description","amount"]). A single full-width order
  alone will drop or stall on wrapped rows.
- "transaction_amount_headers" must name exactly ONE column (the credit/positive
  side when Debit and Credit are separate columns, e.g. ["Credit"]); the opposite
  column goes in "transaction_amount_invert_headers" (e.g. ["Debit"]) with
  "transaction_amount_invert_alignment": "x2" so the engine can locate it.
- "transaction_terms_stop": use SHORT substrings that literally appear in the
  document (e.g. "Transactions stop here."), not full sentences.
- CRITICAL: the statement header date and the TABLE row dates often use different
  formats — e.g. header "1 Jan 2025 to 31 Jan 2025" is format2, while table rows
  print "01 Jan" WITHOUT a year, which is format1. Set "transaction_date_formats"
  from the ROW dates in the layout dump, not from the header line. Likewise the
  table's amount/balance formats follow the ROW text (e.g. "90,350 CR" = format4).
- CRITICAL for the table: each column needs an explicit alignment anchoring it to
  its header — "transaction_date_alignment": "x1" and
  "transaction_description_alignment": "x1" (left edges align), and
  "transaction_amount_alignment"/"transaction_balance_alignment": "x2" (right edges
  align, as amounts do). Without these the engine usually extracts ZERO rows.
  If column edges are ragged, raise "transaction_alignment_tol" (points, default 10).
Alignment values: "y1" same line as the trigger, "x1" same left edge, "x2" same
right edge, "" unconstrained.
The parser fixes what it can itself: implicit dates on continuation rows, missing
balances, year crossovers, amount signs vs the balance column. It VALIDATES that
opening + all amounts = closing; a config that parses one month of a bank should
parse every month."""

EXAMPLE_CONFIG = json.dumps(
    {
        "key": "zz__demo__savings__1",
        "account_terms": ["Demo Savings Bank"],
        "account_number_terms": ["Account number:"],
        "account_number_patterns": [r"\b\d{4} \d{4} \d{4} \d{4}\b"],
        "account_number_alignment": "y1",
        "opening_balance_terms": ["Opening balance:"],
        "opening_balance_formats": ["format1"],
        "opening_balance_alignment": "y1",
        "closing_balance_terms": ["Closing balance:"],
        "closing_balance_formats": ["format1"],
        "closing_balance_alignment": "y1",
        "start_date_terms": ["Statement Period:"],
        "start_date_formats": ["format2"],
        "start_date_alignment": "y1",
        "transaction_terms": ["Transactions"],
        "transaction_terms_stop": ["End of transactions"],
        "transaction_formats": [["date", "description", "amount", "balance"]],
        "transaction_date_formats": ["format5"],
        "transaction_date_headers": ["Date"],
        "transaction_date_alignment": "x1",
        "transaction_description_headers": ["Description"],
        "transaction_description_alignment": "x1",
        "transaction_amount_formats": ["format1"],
        "transaction_amount_headers": ["Amount"],
        "transaction_amount_alignment": "x2",
        "transaction_balance_formats": ["format1"],
        "transaction_balance_headers": ["Balance"],
        "transaction_balance_alignment": "x2",
    },
    indent=2,
)

SYSTEM_PROMPT = f"""You author extraction configurations for the transtractor bank statement
parser. You are given a layout dump of a real statement: each line is
["text", x1, x2, y1, y2] grouped under [Page N] headers; y grows downward.
Your config describes WHERE things are (labels, columns, formats); the
deterministic engine then reads every row exactly. Never transcribe
transactions yourself.

{FIELD_GUIDE}

{FORMAT_CATALOG}

A complete example config for a different (demo) bank:
{EXAMPLE_CONFIG}

Respond with a single JSON object: the configuration only, no prose."""


# --- LLM plumbing (stdlib, zero extra dependencies) --------------------------


def chat(messages: list[dict], max_tokens: int = 1600) -> str:
    body = json.dumps(
        {
            "model": MODEL,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
        }
    ).encode()
    request = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = json.load(response)
    return payload["choices"][0]["message"]["content"]


def extract_json(text: str) -> dict:
    """Tolerant extraction of the first JSON object in a response."""
    start = text.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in response: {text[:200]}")
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\" and in_string:
            escaped = True
        elif char == '"':
            in_string = not in_string
        elif char == "{" and not in_string:
            depth += 1
        elif char == "}" and not in_string:
            depth -= 1
            if depth == 0:
                return json.loads(text[start : index + 1])
    raise ValueError("unbalanced JSON object in response")


# --- Deterministic format suggestions (the harness does the format matching) --

AMOUNT_FORMAT_REGEXES = {
    "format1": r"^-?(\d{1,3}(,\d{3})*|\d+)\.\d{2}-?\s*$",
    "format2": r"^-?\$(\d{1,3}(,\d{3})*|\d+)\.\d{2}-?\s*$",
    "format3": r"^-?\$\d{1,3}(,\d{3})*(\.\d{2})?\s*(CR|DR)$",
    "format4": r"^-?\d{1,3}(,\d{3})*(\.\d{2})?\s*(CR|DR)$",
    "format5": r"^[Nn]il$",
    "format6": r"^[-+]\s*\$\d{1,3}(,\d{3})*$",
    "format7": r"^-?\$\d{1,3}(,\d{3})*(\.\d{2})?\S+$",
}
DATE_FORMAT_REGEXES = {
    "format1": r"^\d{1,2} [A-Za-z]{3,9}$",
    "format2": r"^\d{1,2} [A-Za-z]{3,9} \d{4}$",
    "format3": r"^[A-Za-z]{3,9} \d{1,2},? \d{4}$",
    "format4": r"^\d{1,2}/\d{1,2}/\d{4}$",
    "format5": r"^\d{1,2}/\d{1,2}/\d{2}$",
    "format6": r"^\d{1,2}/\d{1,2}$",
    "format7": r"^\d{1,2}-\d{1,2}-\d{2}(\d{2})?$",
    "format8": r"^\d{1,2}-\d{1,2}-\d{2}(\d{2})?$",
    "format9": r"^\d{1,2}/\d{1,2}/\d{2}(\d{2})?$",
    "format10": r"^[A-Za-z]{3,9} \d{1,2}$",
    "format12": r"^\d{4}/\d{1,2}/\d{1,2}$",
    "format13": r"^\d{4}-\d{1,2}-\d{1,2}$",
}


def format_suggestions(layout_text: str) -> str:
    """Match tokens from the layout dump against every format's regex and
    report which formats the document's own text satisfies, with samples."""
    tokens = re.findall(r'\["([^"]+)"', layout_text)
    lines = []
    for kind, catalog in (("amount", AMOUNT_FORMAT_REGEXES), ("date", DATE_FORMAT_REGEXES)):
        matched = {}
        for token in tokens:
            for name, pattern in catalog.items():
                if re.match(pattern, token.strip()):
                    matched.setdefault(name, set()).add(token.strip())
        for name, samples in sorted(matched.items(), key=lambda kv: -len(kv[1])):
            examples = ", ".join(f"'{s}'" for s in sorted(samples)[:3])
            lines.append(f"- {kind} format {name} matches {len(samples)} tokens, e.g. {examples}")
    return "\n".join(lines)


# --- Deterministic table-shape analysis ---------------------------------------

LAYOUT_ITEM_RE = re.compile(r'\["([^"]*)",-?\d+,-?\d+,-?\d+,-?\d+,-?\d+\]')


def _classify_token(token: str) -> str:
    if any(re.match(p, token) for p in DATE_FORMAT_REGEXES.values()):
        return "date"
    if any(re.match(p, token) for p in AMOUNT_FORMAT_REGEXES.values()):
        return "amount"
    return "description"


HEADER_WORDS = {"Date", "Description", "Amount", "Balance", "Debit", "Credit", "Details"}


def table_shape_analysis(layout_text: str) -> str:
    """Histogram the shapes of lines that look like transaction rows and emit
    the exact transaction_formats orders to declare, with samples. Continuation
    rows (no date, or no balance) are the shapes small models miss."""
    shapes: dict[tuple[str, ...], list[str]] = {}
    role_formats: dict[str, dict[str, set[str]]] = {"amount": {}, "balance": {}}
    in_table = False
    for line in layout_text.splitlines():
        tokens = LAYOUT_ITEM_RE.findall(line)
        if len(tokens) < 2:
            continue
        # Start at the column-header row (Date/Description/Balance/...).
        if not in_table:
            if sum(1 for t in tokens if t in HEADER_WORDS) >= 2:
                in_table = True
            continue
        kinds = [_classify_token(t) for t in tokens]
        money = [t for t, k in zip(tokens, kinds) if k == "amount"]
        amounts = kinds.count("amount")
        if amounts < 1 or kinds.count("description") < 1:
            continue  # header/noise line, not a transaction row
        # With two money columns, the left is the amount and the rightmost the
        # balance; record which formats each column's tokens actually satisfy.
        if amounts >= 2:
            for role, token in (("amount", money[0]), ("balance", money[-1])):
                for name, pattern in AMOUNT_FORMAT_REGEXES.items():
                    if re.match(pattern, token.strip()):
                        role_formats[role].setdefault(name, set()).add(token.strip())
        columns: list[str] = []
        for kind in kinds:
            if kind == "description" and columns and columns[-1] == "description":
                continue  # merge consecutive description tokens
            columns.append(kind)
        if amounts >= 2 and columns.count("amount") == 2:
            # Two money columns: left is the amount, rightmost is the balance.
            last = len(columns) - 1 - columns[::-1].index("amount")
            columns[last] = "balance"
        shapes.setdefault(tuple(columns), []).append(" ".join(tokens)[:60])

    if not shapes:
        return ""
    total = sum(len(v) for v in shapes.values())
    lines = [
        f"Deterministic table analysis: {total} candidate transaction rows found. "
        "Declare ALL of these column orders in transaction_formats (most common first):"
    ]
    for shape, samples in sorted(shapes.items(), key=lambda kv: -len(kv[1])):
        quoted = "[" + ", ".join(f'"{c}"' for c in shape) + "]"
        continuation = (
            " (continuation row — no date)" if "date" not in shape else ""
        )
        lines.append(f"- {quoted} x {len(samples)}{continuation}, e.g. {samples[0]}")
    for role in ("amount", "balance"):
        matches = role_formats[role]
        if matches:
            best = max(matches.items(), key=lambda kv: len(kv[1]))
            examples = ", ".join(f"'{s}'" for s in sorted(best[1])[:3])
            lines.append(
                f"- {role} column tokens ({examples}) match {best[0]} — use that "
                f"in transaction_{role}_formats"
            )
    return "\n".join(lines)


# --- Observation and evaluation ----------------------------------------------


def layout_dump(pdf: Path, max_lines: int = 320) -> str:
    """Render the parser's coordinate layout dump for one statement."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        layout_path = tmp.name
    try:
        Parser().layout(str(pdf), layout_path)
        lines = Path(layout_path).read_text().splitlines()
    finally:
        Path(layout_path).unlink(missing_ok=True)
    if len(lines) > max_lines:
        half = max_lines // 2
        lines = lines[:half] + ["... (middle truncated) ..."] + lines[-half:]
    return "\n".join(lines)


def header_text(pdf: Path, lines: int = 8) -> str:
    """Plain text of the top of page one, for account_terms feedback."""
    return "\n".join(
        re.sub(r"^\[\"(.*?)\",.*$", r"\1", line)
        for line in layout_dump(pdf, max_lines=40).splitlines()
        if line.startswith("[\"")
    )[:lines * 80]


def evaluate(config_json: dict, pdfs: list[Path]) -> dict:
    """Deterministically verify a drafted config against every statement."""
    parser = Parser()
    try:
        parser.load_str(json.dumps(config_json))
    except ConfigLoadError as error:
        return {"stage": "load", "error": str(error), "clean": 0}

    results = []
    for pdf in pdfs:
        try:
            sd = parser.parse(str(pdf))
            results.append(
                {"pdf": pdf.name, "ok": True, "txns": len(sd.transactions)}
            )
        except ParseError as error:
            # Extract how far the engine got: rows + first checker error.
            excerpt = debug_excerpt(pdf, config_json)
            rows = len(re.findall(r"^\s+\d+: ", excerpt, re.M))
            first_error = next(
                (line[5:] for line in excerpt.splitlines() if line.startswith("    - ")),
                str(error)[:120],
            )
            results.append(
                {
                    "pdf": pdf.name,
                    "ok": False,
                    "error": str(error),
                    "rows": rows,
                    "first_error": first_error,
                }
            )
        except Exception as error:  # noqa: BLE001 - engine panics etc. become round feedback
            # Anything else (including pyo3 PanicException from engine
            # invariants) is reported as a failed attempt so the loop can
            # revise instead of crashing.
            results.append(
                {
                    "pdf": pdf.name,
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                    "rows": 0,
                    "first_error": f"{type(error).__name__}: {str(error)[:160]}",
                }
            )
    return {
        "stage": "parse",
        "results": results,
        "clean": sum(1 for r in results if r["ok"]),
        "verified": bool(results) and all(r["ok"] for r in results),
    }


def debug_excerpt(pdf: Path, config_json: dict, limit: int = 2200) -> str:
    """Per-config parse attempt details for a recognised-but-failing config."""
    parser = Parser()
    parser.load_str(json.dumps(config_json))
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        debug_path = tmp.name
    try:
        parser.debug(str(pdf), debug_path)
        return Path(debug_path).read_text()[:limit]
    except ParseError:
        return ""
    finally:
        Path(debug_path).unlink(missing_ok=True)


def table_diagnostics_excerpt(pdf: Path, config_json: dict, limit: int = 2400) -> str:
    """Engine-side per-line table diagnostics for the drafted config."""
    parser = Parser()
    try:
        parser.load_str(json.dumps(config_json))
    except ConfigLoadError:
        return ""
    try:
        return parser.table_diagnostics(str(pdf))[:limit]
    except Exception:  # noqa: BLE001 - diagnostics are best-effort
        return ""


def build_feedback(evaluation: dict, pdfs: list[Path], config_json: dict) -> str:
    if evaluation["stage"] == "load":
        return (
            "The configuration was rejected by validation:\n"
            f"{evaluation['error']}\nFix the named field and return the full corrected config."
        )
    parts = []
    for result in evaluation["results"]:
        if result["ok"]:
            parts.append(f"- {result['pdf']}: parsed cleanly ({result['txns']} transactions).")
        elif "cannot be identified" in result["error"]:
            parts.append(
                f"- {result['pdf']}: NOT IDENTIFIED — no account_terms matched. These "
                f"lines appear at the top of the statement:\n{header_text(pdfs[0])}\n"
                "Pick distinctive words from them for account_terms."
            )
        elif "cannot be parsed" in result["error"]:
            excerpt = debug_excerpt(pdfs[0], config_json)
            diagnostics = table_diagnostics_excerpt(pdfs[0], config_json)
            alignment_hint = ""
            rows = result.get("rows", 0)
            if diagnostics and "lines matching a declared order:" in diagnostics:
                # Most lines match the declared formats but the engine
                # extracted fewer rows: the usual cause is a missing column
                # alignment anchor.
                alignment_hint = (
                    "\nNOTE: most lines already match your formats, yet fewer rows were "
                    "extracted — check that ALL FOUR column alignments are set: "
                    '"transaction_date_alignment": "x1", '
                    '"transaction_description_alignment": "x1", '
                    '"transaction_amount_alignment": "x2", '
                    '"transaction_balance_alignment": "x2". A missing amount/balance '
                    "alignment is the most common cause of rows silently dropping."
                )
            parts.append(
                f"- {result['pdf']}: IDENTIFIED but parsing failed. The engine's "
                "per-line table diagnostics (WHY each line did not match your declared "
                f"column orders/formats):\n{diagnostics or '(unavailable)'}\n"
                f"The engine's debug output for this config was:\n{excerpt or result['error']}\n"
                "Fix exactly what the diagnostics name: add the format it says a token "
                "matches, or the column order it says is missing. A field shown as 'Not "
                "set' means its *_formats id is wrong — compare the EXACT printed text "
                "against the format catalog."
                + alignment_hint
            )
        else:
            parts.append(f"- {result['pdf']}: {result['error']}")
    return (
        "The configuration did not parse every statement cleanly:\n"
        + "\n".join(parts)
        + "\n\nReturn the complete corrected configuration JSON only."
    )


# --- Deterministic column-alignment derivation --------------------------------

LAYOUT_ITEM_XY_RE = re.compile(r'\["([^"]*)",(-?\d+),(-?\d+),(-?\d+),(-?\d+),(-?\d+)\]')


def derive_alignments(config: dict, layout_text: str) -> list[str]:
    """Derive money-column alignments from the table's own geometry.

    Finds the column-header line, then votes: for each multi-money line, does
    the money token's left edge (x1) or right edge (x2) sit within tolerance
    of its header's edge? The winning edge per column is written into the
    config. This is the fix for the classic silent row-killer: a money column
    anchored to the wrong edge mis-binds and drops rows.
    """
    lines = [
        LAYOUT_ITEM_XY_RE.findall(line)
        for line in layout_text.splitlines()
        if line.startswith('["')
    ]
    tol = int(config.get("transaction_alignment_tol", 10))

    def find_header_edge(headers: list[str]) -> tuple[int, int] | None:
        for line in lines:
            for text, x1, x2, *_ in line:
                if any(h in text for h in headers):
                    return int(x1), int(x2)
        return None

    amount_edge = find_header_edge(config.get("transaction_amount_headers", []))
    balance_edge = find_header_edge(config.get("transaction_balance_headers", []))
    if amount_edge is None and balance_edge is None:
        return []

    def money_like(token: str) -> bool:
        return any(
            re.match(pattern, token.strip())
            for pattern in AMOUNT_FORMAT_REGEXES.values()
        )

    votes = {"amount": [0, 0], "balance": [0, 0]}  # [x1, x2]
    for line in lines:
        money = [
            (int(x1), int(x2))
            for text, x1, x2, *_ in line
            if money_like(text)
        ]
        if len(money) < 2:
            continue
        for column, token, edge in (
            ("amount", money[0], amount_edge),
            ("balance", money[-1], balance_edge),
        ):
            if edge is None:
                continue
            if abs(token[0] - edge[0]) <= tol:
                votes[column][0] += 1
            if abs(token[1] - edge[1]) <= tol:
                votes[column][1] += 1

    applied = []
    for column in ("amount", "balance"):
        x1_votes, x2_votes = votes[column]
        winner = "x2" if x2_votes > x1_votes else "x1" if x1_votes > x2_votes else None
        if winner and config.get(f"transaction_{column}_alignment") != winner:
            config[f"transaction_{column}_alignment"] = winner
            applied.append(
                f"transaction_{column}_alignment={winner} "
                f"(x2 votes {x2_votes} vs x1 {x1_votes})"
            )
    return applied


# --- The loop ----------------------------------------------------------------


def draft_config(pdfs: list[Path], rounds: int) -> tuple[dict | None, bool, list[dict]]:
    """Returns (best config or None, verified?, round history)."""
    observations = "\n\n".join(
        f"--- Statement: {pdf.name} ---\n{layout_dump(pdf)}" for pdf in pdfs
    )
    suggestions = format_suggestions(observations)
    if suggestions:
        observations += (
            "\n\nDeterministic format analysis of the tokens above (use these "
            f"format ids):\n{suggestions}"
        )
    shapes = table_shape_analysis(observations)
    if shapes:
        observations += f"\n\n{shapes}"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Layout dumps:\n{observations}\n\nWrite the configuration."},
    ]
    best: tuple[int, dict] | None = None
    history: list[dict] = []
    previous_config: dict | None = None

    for round_number in range(rounds):
        started = time.perf_counter()
        try:
            config_json = extract_json(chat(messages))
        except ValueError as error:
            print(f"round {round_number}: LLM produced unusable output ({error}); retrying")
            messages.append({"role": "assistant", "content": "(invalid JSON)"})
            messages.append({"role": "user", "content": "Return only the JSON object."})
            continue

        # Deterministic pre-pass: derive money-column alignments from the
        # table geometry itself — never ask the model what arithmetic can
        # decide.
        applied = derive_alignments(config_json, observations)

        evaluation = evaluate(config_json, pdfs)
        elapsed = time.perf_counter() - started
        Path("last_draft.json").write_text(json.dumps(config_json, indent=2))

        changed = ""
        if previous_config is not None:
            diff = [
                k
                for k in set(config_json) | set(previous_config)
                if config_json.get(k) != previous_config.get(k)
            ]
            changed = f" changed: {sorted(diff)}" if diff else " (unchanged!)"
        previous_config = config_json

        summary = (
            "VERIFIED"
            if evaluation.get("verified")
            else f"{evaluation.get('clean', 0)}/{len(pdfs)} clean"
            if evaluation["stage"] == "parse"
            else f"rejected at validation: {evaluation['error'][:90]}"
        )
        detail = ""
        if evaluation["stage"] == "parse" and not evaluation.get("verified"):
            for result in evaluation["results"]:
                if not result["ok"]:
                    detail = (
                        f" | rows={result.get('rows', '?')} "
                        f"| {result.get('first_error', '')[:100]}"
                    )
                    break
        key = config_json.get("key", "?")
        auto = f" | auto-aligned: {', '.join(applied)}" if applied else ""
        print(
            f"round {round_number}: key={key} -> {summary} ({elapsed:.1f}s)"
            f"{detail}{changed}{auto}"
        )
        history.append({"round": round_number, "key": key, "outcome": summary})

        if evaluation.get("verified"):
            return config_json, True, history
        # Only keep configs that parsed at least one statement cleanly.
        if evaluation["stage"] == "parse" and evaluation["clean"] > 0:
            if best is None or evaluation["clean"] > best[0]:
                best = (evaluation["clean"], config_json)

        messages.append({"role": "assistant", "content": json.dumps(config_json)})
        feedback = build_feedback(evaluation, pdfs, config_json)
        if changed == " (unchanged!)":
            # The model parroted its previous answer in the long conversation.
            # Escalate: reset to a short, direct instruction with the specific
            # fix named — small models comply with direct edits, not essays.
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "This configuration failed to parse the statement:\n"
                        f"{json.dumps(config_json)}\n\nDiagnosis: "
                        f"{feedback[-1200:]}\n\nApply the named fixes and return the "
                        "complete corrected configuration JSON. Every column MUST have "
                        'an alignment: "transaction_date_alignment": "x1", '
                        '"transaction_description_alignment": "x1", '
                        '"transaction_amount_alignment": "x2", '
                        '"transaction_balance_alignment": "x2".'
                    ),
                },
            ]
        else:
            messages.append({"role": "user", "content": feedback})

    return (best[1] if best else None), False, history


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("pdfs", nargs="+", type=Path, help="statements from one bank")
    cli.add_argument(
        "--rounds",
        type=int,
        default=ROUNDS,
        help=f"correction turns for the drafting loop (default {ROUNDS}, from "
        "TRANSTRACTOR_LLM_ROUNDS or .env)",
    )
    args = cli.parse_args()

    print(f"config harness: {len(args.pdfs)} statement(s), {MODEL} at {BASE_URL}")
    config, verified, history = draft_config(args.pdfs, args.rounds)
    if config is None:
        print("\nFAILED: no configuration parsed any statement cleanly.")
        print(json.dumps(history, indent=2))
        return 1
    if not verified:
        print("\nFAILED: best config parsed some, not all, statements cleanly.")
        print(json.dumps(history, indent=2))
        return 1

    REGISTRY.mkdir(exist_ok=True)
    out_path = REGISTRY / f"{config.get('key', 'drafted')}.json"
    out_path.write_text(json.dumps(config, indent=2))
    print(f"\nVERIFIED config saved: {out_path}")
    print("Reuse with zero LLM:")
    print(f"  parser = Parser(); parser.load({str(out_path)!r}); parser.parse('statement.pdf')")
    # Final deterministic confirmation with a fresh parser:
    parser = Parser()
    parser.load(str(out_path))
    for pdf in args.pdfs:
        sd = parser.parse(str(pdf))
        print(
            f"  {pdf.name}: {len(sd.transactions)} transactions, "
            f"opening {sd.opening_balance:.2f} -> closing {sd.closing_balance:.2f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
