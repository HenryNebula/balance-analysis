"""Engine-emulating probe library for the REPL-distill harness.

The model's script imports this module and works against the statement's
pre-extracted layout FRAMES (one frames.json per run directory, written by
distill_harness.py). Two tiers of use:

- probes: show/lines_with/header_line/classify/parse_amount/parse_date/
  reconcile — free-form exploration of the layout, mirroring how the rules
  engine itself reads a statement (items with coordinates, y1 anchors,
  x1/x2 column alignment, engine amount/date format ids).
- the form: build_config(**fields) — the ONLY thing that persists. Its
  keyword arguments are exactly the config fields the real engine accepts;
  distillation is reading them back out of the call.

The emulator deliberately mirrors the engine's semantics (same format ids,
same alignment meanings, CR/DR as sign, word-level tokenisation) so that
parameters the model verifies here behave the same in the engine — but the
engine stays the authority: a distilled config is only ever accepted after
it parses the real statements cleanly through Parser(rules_only=True).

stdlib only; runs inside the sandboxed child process.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# --- Engine format semantics (mirrors src/formats/* and the config guide) ----

# Shape gate per declared amount format (the classification regexes the
# upstream harness uses; format6 follows the engine's signed-currency word).
AMOUNT_FORMAT_REGEXES = {
    "format1": r"^-?\d{1,3}(,\d{3})*\.\d{2}-?\s*$",
    "format2": r"^-?\$(\d{1,3}(,\d{3})*|\d+)\.\d{2}-?\s*$",
    "format3": r"^-?\$\d{1,3}(,\d{3})*(\.\d{2})?\s*(CR|DR)$",
    "format4": r"^-?\d{1,3}(,\d{3})*(\.\d{2})?\s*(CR|DR)$",
    "format5": r"^[Nn]il$",
    "format6": r"^([+-])\s+\$([\d,]+\.\d{2})$",
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
CR_DR_ITEMS = {"cr", "dr"}

# --- Config field vocabulary (what build_config accepts) ----------------------

ACCOUNT_TYPES = {
    "Checking", "Savings", "Credit Card", "Investment", "Loan", "Mortgage",
    "Mixed", "Other",
}
COLUMN_NAMES = {"date", "description", "amount", "balance"}
ALIGNMENTS = {"y1", "x1", "x2", ""}
AMOUNT_FIELDS = {
    "opening_balance_formats", "closing_balance_formats",
    "transaction_amount_formats", "transaction_balance_formats",
}
DATE_FIELDS = {"start_date_formats", "transaction_date_formats"}
ALIGNMENT_FIELDS = {
    "account_number_alignment", "opening_balance_alignment",
    "closing_balance_alignment", "start_date_alignment",
    "transaction_date_alignment", "transaction_description_alignment",
    "transaction_amount_alignment", "transaction_balance_alignment",
    "transaction_amount_invert_alignment",
}
LIST_FIELDS = {
    "account_terms", "account_number_terms", "account_number_patterns",
    "opening_balance_terms", "closing_balance_terms", "start_date_terms",
    "transaction_terms", "transaction_terms_stop",
    "transaction_date_headers", "transaction_description_headers",
    "transaction_amount_headers", "transaction_amount_invert_headers",
    "transaction_balance_headers",
}
STR_FIELDS = {"bank_name"}
KEY_RE = re.compile(r"^[a-z]{2}__[a-z0-9_]+__[a-z0-9_]+__\d+$")


# --- Frames -------------------------------------------------------------------


def load_frames() -> list[dict]:
    """The run's statements: [{name, lines: [{page, y, items: [[text,x1,x2],..]}]}].

    Read from frames.json in the working directory (written by the harness).
    Print with show(); probe with lines_with()/header_line()/classify().
    """
    data = json.loads(Path("frames.json").read_text())
    return data["statements"]


def line_text(line: dict) -> str:
    return " ".join(item[0] for item in line["items"])


def show(lines: list[dict], n: int = 15, start: int = 0) -> None:
    """Print lines[start:start+n] in layout-dump form, with line indices."""
    page = None
    for index in range(start, min(start + n, len(lines))):
        line = lines[index]
        if line["page"] != page:
            page = line["page"]
            print(f"[Page {page}]")
        items = " ".join(f'["{t}",{x1},{x2}]' for t, x1, x2 in line["items"])
        print(f"{index:03d} {items}")


def lines_with(term: str, lines: list[dict]) -> list[dict]:
    """Lines whose text contains `term` (substring, case-sensitive — the
    engine's *_terms match the same way)."""
    return [line for line in lines if term in line_text(line)]


def values_on_line(line: dict, term: str) -> list[str]:
    """Item texts to the RIGHT of the item containing `term` on its line —
    the y1-alignment semantic (value printed on the same line as the label)."""
    right = []
    after = False
    for text, x1, _x2 in line["items"]:
        if after:
            right.append(text)
        if term in text:
            after = True
    return right


def anchor_value(lines: list[dict], term: str, index: int = 0) -> str:
    """The printed value beside a label, as one string —
    anchor_value(lines, "Account number:") -> "1234 5678 9123 4567".
    Combines lines_with + values_on_line so you never index line/item
    structures by hand. Raises with the printed line when nothing sits
    beside the label."""
    for line in lines:
        if term in line_text(line):
            values = values_on_line(line, term)
            if values:
                return " ".join(values)
            raise ValueError(
                f"nothing is printed beside {term!r} on its line "
                f"(line: {line_text(line)!r}) — is the label right?"
            )
    raise ValueError(f"no line contains {term!r}")


def header_line(lines: list[dict], *words: str, need: int = 2) -> dict | None:
    """First line containing at least `need` of the given header words
    (e.g. header_line(lines, "Date", "Description", "Balance"))."""
    for line in lines:
        if sum(1 for item in line["items"] if item[0] in words) >= need:
            return line
    return None


def spans(line: dict) -> dict[str, tuple[int, int]]:
    """{header text: (x1, x2)} for every item on a header line."""
    return {item[0]: (item[1], item[2]) for item in line["items"]}


# --- Table classification -----------------------------------------------------


def region(
    lines: list[dict], start_term: str, stop_terms: tuple[str, ...] | list[str] = ()
) -> tuple[int, int]:
    """(first, last+1) line indices of the table: after the line containing
    start_term, before the first line containing any stop term."""
    first = next(
        (i for i, line in enumerate(lines) if start_term in line_text(line)), 0
    )
    last = len(lines)
    for i in range(first + 1, len(lines)):
        if any(term in line_text(lines[i]) for term in stop_terms):
            last = i
            break
    return first + 1, last


def classify(
    lines: list[dict],
    column_spans: dict[str, tuple[int, int]],
    first: int = 0,
    last: int | None = None,
) -> tuple[list[dict], list[tuple[str, ...]]]:
    """Assign every item on lines[first:last] to the column whose span its
    center falls in (nearest span center wins — the x1/x2 alignment the
    engine enforces, viewed geometrically).

    Returns (rows, orders): rows are {"#": line index, role: [texts..]} with
    an empty list for absent roles (continuation rows); orders[i] is the
    role sequence of row i in print order — the shapes to declare in
    transaction_formats. Roles are your own keys of column_spans (e.g.
    "date", "description", "debit", "amount", "balance").
    """
    if last is None:
        last = len(lines)
    centers = {
        role: (x1 + x2) / 2 for role, (x1, x2) in column_spans.items()
    }
    rows: list[dict] = []
    orders: list[tuple[str, ...]] = []
    for index in range(first, min(last, len(lines))):
        row = {"#": index}
        order: list[str] = []
        for text, _x1, _x2 in lines[index]["items"]:
            center = (_x1 + _x2) / 2
            role = min(centers, key=lambda r: abs(centers[r] - center))
            row.setdefault(role, []).append(text)
            if not order or order[-1] != role:
                order.append(role)
        for role in column_spans:
            row.setdefault(role, [])
        rows.append(row)
        orders.append(tuple(order))
    return rows, orders


# --- Format parsing (engine semantics) -----------------------------------------


def _as_words(words: list[str] | str, index: int = 0) -> list[str]:
    """Flatten to the word sequence the engine sees: a string is split on
    whitespace, and a list of item texts is split item by item (the engine
    tokenises every printed item into words inheriting its coordinates)."""
    if isinstance(words, str):
        return words.split()
    if words is None:
        raise ValueError(
            "no token to parse — the value is None (did that role exist on "
            "the row? continuation rows carry [] for absent roles)"
        )
    return [word for item in words for word in str(item).split()]


def amount_format_of(words: list[str] | str, index: int = 0) -> str | None:
    """The engine amount format id that can parse the amount starting at
    words[index], or None when NO engine format can (missing comma grouping
    above 999, missing cents without CR/DR). Accepts a string or a list of
    item texts (flattened to words). CR/DR-suffixed values may omit cents
    (format3/format4); the suffix may be glued or a separate word."""
    seq = _as_words(words)
    word = seq[index].strip()
    next_is_crdr = (
        index + 1 < len(seq) and seq[index + 1].strip().lower() in CR_DR_ITEMS
    )
    crdr = next_is_crdr or bool(re.search(r"(?:CR|DR)$", word, re.I))
    candidate = f"{word} {seq[index + 1].strip()}" if next_is_crdr else word
    plain_crdr = r"^-?\d{1,3}(,\d{3})*(\.\d{2})?\s*(CR|DR)$"
    currency_crdr = r"^-?\$\d{1,3}(,\d{3})*(\.\d{2})?\s*(CR|DR)$"
    plain = r"^-?\d{1,3}(,\d{3})*\.\d{2}$"
    currency = r"^-?\$\d{1,3}(,\d{3})*\.\d{2}$"
    if crdr:
        if re.match(currency_crdr, candidate, re.I):
            return "format3"
        if re.match(plain_crdr, candidate, re.I):
            return "format4"
    if re.match(plain, word):
        return "format1"
    if re.match(currency, word):
        return "format2"
    if re.match(r"^[Nn]il$", word):
        return "format5"
    if re.match(r"^([+-])\s+\$([\d,]+\.\d{2})$", word):
        return "format6"
    if word.endswith("⧫") and re.match(currency, word[:-1].strip()):
        return "format7"
    return None


def parse_amount(
    words: list[str] | str, fmt: str | None, index: int = 0
) -> float | None:
    """Signed value of the amount starting at words[index] under the declared
    engine format, or None if the format cannot read it. Accepts a string
    (split on whitespace) or a list of item texts (flattened to words) —
    format3/format4 consume the number word PLUS a following CR/DR word,
    exactly like the engine. CR is positive, DR negative; trailing '-' also
    negates. fmt=None auto-detects via amount_format_of (and returns None
    when no engine format can read the token — e.g. ungrouped digits)."""
    if fmt is None:
        fmt = amount_format_of(words, index)
        if fmt is None:
            return None
    if fmt not in AMOUNT_FORMAT_REGEXES:
        raise ValueError(
            f"unknown amount format {fmt!r}; valid: {sorted(AMOUNT_FORMAT_REGEXES)} "
            "(derive one with amount_format_of)"
        )
    seq = _as_words(words)
    word = seq[index].strip()
    # Glue or separate CR/DR word for the suffixed formats.
    if fmt in ("format3", "format4") and index + 1 < len(seq):
        if seq[index + 1].strip().lower() in CR_DR_ITEMS and not (
            word.upper().endswith(("CR", "DR"))
        ):
            word = f"{word} {seq[index + 1].strip().upper()}"
    if not re.match(AMOUNT_FORMAT_REGEXES[fmt], word):
        return None
    if fmt == "format5":
        return 0.0
    negative = False
    if word.upper().endswith("DR"):
        negative = True
        word = word[:-2].strip()
    elif word.upper().endswith("CR"):
        word = word[:-2].strip()
    digits = word.replace("$", "").replace(",", "").replace(" ", "")
    trailing_minus = digits.endswith("-")
    digits = digits.strip("+-")
    value = float(digits)
    if trailing_minus or (digits.startswith("-") and value < 0):
        negative = True
        value = -value
    return -value if negative else value


def date_format_of(token: str) -> list[str]:
    """Date format ids whose shape the token matches (often several — pick the
    one matching how the statement actually prints)."""
    text = token.strip()
    return [
        name
        for name, pattern in DATE_FORMAT_REGEXES.items()
        if re.match(pattern, text)
    ]


MONTH_WORDS = {
    prefix: number
    for number, name in enumerate(
        [
            "january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december",
        ],
        start=1,
    )
    for prefix in (name[:3],)
}


def parse_date(token: str, year: int | None = None) -> tuple[int | None, int, int] | None:
    """(year, month, day) reading of a date token; year is None when the
    token prints none (the engine infers it — continuation rows inherit).
    Returns None when the token is not date-shaped."""
    text = token.strip().rstrip(",")
    if not any(re.match(p, text) for p in DATE_FORMAT_REGEXES.values()):
        return None
    parts = re.findall(r"[A-Za-z]+|\d+", text)
    alpha = [p for p in parts if p.isalpha()]
    nums = [int(p) for p in parts if p.isdigit()]
    if alpha:
        month = MONTH_WORDS.get(alpha[0].lower()[:3])
        if month is None or not nums:
            return None
        y = nums[1] if len(nums) > 1 and nums[1] >= 1000 else None
        return (y, month, nums[0])
    if len(nums) >= 2:
        if len(nums) == 3:
            if nums[2] >= 1000:
                return (nums[2], nums[0], nums[1])
            if nums[0] >= 1000:
                return (nums[0], nums[1], nums[2])
        return (None, nums[0], nums[1])
    return None


def _coerce_amount(value, what: str) -> float:
    """Accept a float, a printed token string, or a list of item texts."""
    if isinstance(value, (int, float)):
        return float(value)
    parsed = parse_amount(value, None)
    if parsed is None:
        raise ValueError(
            f"{what} {value!r} is not a number and matches no engine amount "
            "format — pass the value parse_amount() returned, or a token it "
            "can read"
        )
    return parsed


def signed_amounts(
    rows: list[dict], credit: str = "amount", debit: str = "debit", fmt: str | None = None
) -> list[float]:
    """Signed amount per classified row: (credit value or 0) - (debit value
    or 0), parsed with fmt (auto-detected per token when omitted). Rows with
    no money contribute 0.0 — wrapped descriptions. This is arithmetic, not
    a judgement call: use its output for reconcile()."""
    amounts = []
    for row in rows:
        value = 0.0
        for tokens in row.get(credit, []) or []:
            parsed = parse_amount(tokens, fmt)
            if parsed is not None:
                value += parsed
        for tokens in row.get(debit, []) or []:
            parsed = parse_amount(tokens, fmt)
            if parsed is not None:
                value -= parsed
        amounts.append(value)
    return amounts


def reconcile(
    amounts: list, opening, closing, tol: float = 0.01
) -> bool:
    """Local mirror of the engine's balance checkers: opening + signed
    amounts must equal closing. Accepts floats or printed tokens (parsed
    with an auto-detected format). Prints the verdict. ALWAYS run this on
    the full signed amount column before calling build_config — it is the
    cheapest signal that roles/formats are right."""
    global _LAST_RECONCILE_OK
    opening = _coerce_amount(opening, "opening")
    closing = _coerce_amount(closing, "closing")
    amounts = [_coerce_amount(a, "amount") for a in amounts]
    running = opening + sum(amounts)
    drift = round(running - closing, 2)
    if abs(drift) <= tol:
        print(f"reconcile: OK (opening {opening:,.2f} -> closing {closing:,.2f})")
        _LAST_RECONCILE_OK = True
        return True
    print(
        f"reconcile: FAIL — opening {opening:,.2f} + amounts = {running:,.2f}, "
        f"closing printed {closing:,.2f} (drift {drift:+,.2f}; check for a "
        "dropped row, a wrong sign, or a wrong column role)"
    )
    _LAST_RECONCILE_OK = False
    return False


def pattern_for(token: str) -> str:
    """A regex matching the printed token's SHAPE: every digit run becomes
    \\d{run-length}, everything else is escaped. Derive account_number_patterns
    from the value printed beside the label — e.g. "1234 5678 9123 4567"
    -> "\\d{4} \\d{4} \\d{4} \\d{4}". Never hand-invent a pattern for a value
    you can see."""
    pattern = ""
    for run in re.finditer(r"\d+|\D+", token.strip()):
        text = run.group(0)
        if text[0].isdigit():
            pattern += f"\\d{{{len(text)}}}"
        else:
            pattern += re.escape(text).replace("\\ ", " ")
    return pattern


def transaction_shapes(orders: list[tuple[str, ...]]) -> list[list[str]]:
    """Role sequences from classify() -> config transaction_formats orders.

    Mechanical mapping so the config's column vocabulary is never hand-built:
    debit/credit roles map to the single "amount" column (the sign split is
    expressed by invert headers, never by column names), adjacent duplicate
    roles collapse (two tokens classified into one role bind once), and
    shapes that bind no money are dropped — those lines are wrapped-
    description continuations the engine merges by itself. Most frequent
    shape first."""
    counts: dict[tuple[str, ...], int] = {}
    for order in orders:
        shape: list[str] = []
        for role in order:
            mapped = "amount" if role in ("debit", "credit", "amount") else role
            if not shape or shape[-1] != mapped:
                shape.append(mapped)
        if "amount" in shape:
            counts[tuple(shape)] = counts.get(tuple(shape), 0) + 1
    return [list(shape) for shape in sorted(counts, key=counts.get, reverse=True)]


# --- The form: the only thing that persists -----------------------------------


_LAST_RECONCILE_OK: bool | None = None


def build_config(**fields) -> dict:
    """Validate and emit the config; ALSO writes it to config.json in the
    working directory (the harness reads that file — this call is the
    distillation). Keyword arguments are exactly the engine's config fields;
    invalid values raise ValueError naming the valid options. Call it LAST,
    with the values you verified via the probes above.

    Refuses to run when the last emu.reconcile() printed FAIL: a config
    whose own local balance check fails cannot be distilled (the engine
    gate would reject it anyway — this fails one step earlier, with the
    drift in the traceback)."""
    if _LAST_RECONCILE_OK is False:
        raise ValueError(
            "build_config blocked: your last emu.reconcile() FAILED — the "
            "region/roles/formats are not right yet (a stop term that cuts "
            "the table early or too late is the usual cause). Fix it until "
            "reconcile prints OK, then call build_config."
        )
    for name in fields:
        if not (
            name in AMOUNT_FIELDS
            or name in DATE_FIELDS
            or name in ALIGNMENT_FIELDS
            or name in LIST_FIELDS
            or name in STR_FIELDS
            or name in ("key", "account_type", "transaction_formats",
                        "transaction_alignment_tol")
        ):
            raise ValueError(
                f"unknown field {name!r}; valid fields: key, account_type, "
                f"bank_name, any of {sorted(LIST_FIELDS | ALIGNMENT_FIELDS | AMOUNT_FIELDS | DATE_FIELDS | STR_FIELDS)}, "
                "transaction_formats, transaction_alignment_tol"
            )
    if "key" not in fields or not KEY_RE.match(str(fields["key"])):
        raise ValueError(
            'key must be "<country>__<bank>__<product>__<version>" e.g. '
            '"us__demo__savings__1" (lowercase, two-letter country)'
        )
    if "account_type" in fields and fields["account_type"] not in ACCOUNT_TYPES:
        raise ValueError(
            f"account_type must be one of {sorted(ACCOUNT_TYPES)}"
        )
    for name in AMOUNT_FIELDS & fields.keys():
        bad = [f for f in fields[name] if f not in AMOUNT_FORMAT_REGEXES]
        if bad:
            raise ValueError(
                f"{name}: unknown amount format(s) {bad}; valid: "
                f"{sorted(AMOUNT_FORMAT_REGEXES)}"
            )
    for name in DATE_FIELDS & fields.keys():
        bad = [f for f in fields[name] if f not in DATE_FORMAT_REGEXES]
        if bad:
            raise ValueError(
                f"{name}: unknown date format(s) {bad}; valid: "
                f"{sorted(DATE_FORMAT_REGEXES)}"
            )
    for name in ALIGNMENT_FIELDS & fields.keys():
        if fields[name] not in ALIGNMENTS:
            raise ValueError(
                f"{name}: {fields[name]!r} not in {sorted(ALIGNMENTS)}"
            )
    if "transaction_formats" in fields:
        for order in fields["transaction_formats"]:
            bad = [c for c in order if c not in COLUMN_NAMES]
            if bad:
                raise ValueError(
                    f"transaction_formats column(s) {bad} invalid; only "
                    f"{sorted(COLUMN_NAMES)} (never debit/credit — model "
                    "separate columns via amount + invert headers)"
                )
    for name in LIST_FIELDS & fields.keys():
        if not isinstance(fields[name], list) or not fields[name]:
            raise ValueError(f"{name} must be a non-empty list")
    config = dict(fields)
    Path("config.json").write_text(json.dumps(config, indent=2))
    return config
