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


# --- Layout primitives: plain data + freedom ------------------------------------
#
# The shaped helpers (table, classify, region) are POLICIES — conveniences
# for the common ledger. When a statement's shape doesn't fit one, the model
# should drop to these PRIMITIVES and plain python/re, not force the tool.

def tokens(lines: list[dict]) -> list[tuple]:
    """Flat printed-item stream with geometry: (text, x1, x2, y, page,
    line_index). The substrate — loop it, filter it, regex it, cluster it."""
    stream = []
    for index, line in enumerate(lines):
        for text, x1, x2 in line["items"]:
            stream.append((text, x1, x2, line["y"], line["page"], index))
    return stream


def cluster_x(items: list[tuple], gap: float = 20) -> list[tuple[float, list]]:
    """1-D clustering of items by x-center into column groups:
    [(center, [items...]), ...] left to right. Use it to find column
    structure the header row didn't declare cleanly (wide or irregular
    layouts, multi-line description columns)."""
    ordered = sorted(items, key=lambda t: (t[1] + t[2]) / 2)
    groups: list[list] = []
    for item in ordered:
        center = (item[1] + item[2]) / 2
        if groups and center - groups[-1][0] <= gap:
            group = groups[-1]
            group[0] = (group[0] * len(group[1]) + center) / (len(group[1]) + 1)
            group[1].append(item)
        else:
            groups.append([center, [item]])
    return [(round(center, 1), items_) for center, items_ in groups]


def date_format_histogram(texts: list[str]) -> dict[str | None, list[str]]:
    """Which engine date format ids cover the given samples:
    {format id: [matching samples]} (plus None: [unmatchable]). Pick
    *_formats values from this evidence, never by guessing."""
    histogram: dict[str | None, list[str]] = {}
    for text in texts:
        matches = date_format_of(str(text))
        if matches:
            for name in matches:
                histogram.setdefault(name, []).append(str(text))
        else:
            histogram.setdefault(None, []).append(str(text))
    return dict(sorted(histogram.items(), key=lambda kv: -len(kv[1])))


def amount_format_histogram(texts: list[str]) -> dict[str | None, list[str]]:
    """Which engine amount format ids cover the given samples:
    {format id: [matching samples]} (plus None: [unmatchable — no engine
    format can read them])."""
    histogram: dict[str | None, list[str]] = {}
    for text in texts:
        name = amount_format_of([str(text)])
        histogram.setdefault(name, []).append(str(text))
    return dict(sorted(histogram.items(), key=lambda kv: -len(kv[1])))


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


def section_markers(
    lines: list[dict], markers: dict[str, str]
) -> list[tuple[int, str]]:
    """(line index, marker name) for every line containing one of the given
    section markers, in document order — e.g.
    section_markers(lines, {"withdrawals": "Withdrawals and other subtractions",
                            "deposits": "Deposits and other additions"}).
    Sectioned statements are common in the wild; classify your table per
    section and pass per-section signed amounts to reconcile()."""
    hits: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        text = line_text(line)
        for name, marker in markers.items():
            if marker in text:
                hits.append((index, name))
                break
    return hits


# --- Grid materialization: the ledger IS a table ---------------------------------
#
# Spatial reasoning is the harness's job, not the model's. A ledger's header
# row defines the columns; every data cell falls under one. table() projects
# the geometry onto that grid deterministically and returns regular rows.

COLUMN_WORD_RE = re.compile(
    r"^(date|posted|description|details|detail|amount|balance|debit|credit|"
    r"withdrawals?|deposits?|type|ref|reference|no\.?|chq|cheque|value)$",
    re.I,
)


def _moneyish(cell: str) -> bool:
    if not cell.strip():
        return False
    words = cell.split()
    return amount_format_of(words) is not None or bool(
        re.match(r"(?i)^[-+]?\$?[\d,]+(\.\d+)?$", cell.strip())
    )


def find_header(lines: list[dict]) -> dict | None:
    """The most header-like line: most items that are exactly common column
    words (Date, Description, Amount, Balance, Debit, Credit, ...)."""
    best, best_score = None, 0
    for line in lines:
        score = sum(
            1 for item in line["items"] if COLUMN_WORD_RE.match(item[0].strip())
        )
        if score >= 2 and score > best_score:
            best, best_score = line, score
    return best


def page_furniture(lines: list[dict]) -> set[int]:
    """Indexes of repeated page furniture — lines whose full text recurs at
    the same y on two or more pages (running headers/footers). The engine
    drops these before parsing; the grid must too, or they masquerade as
    continuation rows and pollute the Date column."""
    seen: dict[tuple[str, int], set[int]] = {}
    for index, line in enumerate(lines):
        key = (line_text(line), line["y"])
        seen.setdefault(key, set()).add(line["page"])
    furniture = set()
    for (text, y), pages in seen.items():
        if len(pages) >= 2:
            for index, line in enumerate(lines):
                if line_text(line) == text and line["y"] == y:
                    furniture.add(index)
    return furniture


def table(
    lines: list[dict],
    header: dict | None = None,
    start: int | None = None,
    stop: int | None = None,
    stop_terms: tuple[str, ...] | list[str] = (),
    sections: dict[str, str] | None = None,
    merge_continuations: bool = True,
) -> tuple[list[str], list[dict]]:
    """Materialize the ledger grid: (columns, rows).

    - header: the header line (auto-detected by find_header() when omitted)
    - start/stop: data line range (default: after the header, to the first
      line containing any stop term, else end of lines)
    - sections: {label: marker} — every row carries "_section" set to the
      label of the last marker line at or above it (sectioned statements:
      withdrawals/deposits blocks over one shared column set)
    - merge_continuations: rows with no date and no money merge their
      description into the previous row (wrapped descriptions)

    Column boundaries are the midpoints between adjacent header item
    centers, edge columns open-ended — the grid a human sees. rows[i] maps
    every column header to its cell text ("" when empty)."""
    header = header or find_header(lines)
    if header is None:
        raise ValueError(
            "no header row found — pass header=emu.header_line(lines, ...)"
        )
    heads = [(text.strip(), (x1 + x2) / 2) for text, x1, x2 in header["items"]]
    heads.sort(key=lambda h: h[1])
    columns = [text for text, _ in heads]
    centers = [center for _, center in heads]
    bounds = [
        (float("-inf") if i == 0 else (centers[i - 1] + centers[i]) / 2,
         float("inf") if i == len(centers) - 1 else (centers[i] + centers[i + 1]) / 2)
        for i in range(len(centers))
    ]

    first = start if start is not None else lines.index(header) + 1
    last = stop if stop is not None else len(lines)
    if stop_terms and stop is None:
        for i in range(first, len(lines)):
            if any(term in line_text(lines[i]) for term in stop_terms):
                last = min(last, i)
                break

    section_of: str | None = None
    furniture = page_furniture(lines)
    raw: list[dict] = []
    for index in range(first, min(last, len(lines))):
        if index in furniture:
            continue
        line = lines[index]
        text = line_text(line)
        if sections:
            for label, marker in sections.items():
                if marker in text:
                    section_of = label
                    break
        row = {column: "" for column in columns}
        for item_text, x1, x2 in line["items"]:
            center = (x1 + x2) / 2
            for column, (low, high) in zip(columns, bounds):
                if low <= center < high:
                    row[column] = (row[column] + " " + item_text).strip()
                    break
        row["_line"] = index
        if sections:
            row["_section"] = section_of
        if not any(row[c] for c in columns):
            continue  # blank line
        if merge_continuations and raw:
            has_money = any(_moneyish(row[c]) for c in columns)
            has_date = any(
                date_format_of(token)
                for c in columns
                for token in row[c].split()
            )
            desc = next(
                (c for c in columns if "desc" in c.lower() or "detail" in c.lower()),
                None,
            )
            if not has_money and not has_date and desc and row[desc]:
                raw[-1][desc] = (raw[-1][desc] + " " + row[desc]).strip()
                for c in columns:  # keep any stray non-description content
                    if c != desc and row[c]:
                        raw[-1][c] = (raw[-1][c] + " " + row[c]).strip()
                continue
        raw.append(row)
    return columns, raw


def render_table(columns: list[str], rows: list[dict], width: int = 28) -> None:
    """Print the materialized grid as an aligned table — read it like a page."""
    def cell(row, column):
        text = str(row.get(column, ""))
        return (text[: width - 1] + "…") if len(text) > width else text

    cols = [c for c in columns if any(str(r.get(c, "")).strip() for r in rows)] + [
        c for c in ("_section",) if any(r.get("_section") for r in rows)
    ]
    sizes = [max(len(c), *(len(cell(r, c)) for r in rows)) if rows else len(c) for c in cols]
    print(" | ".join(c.ljust(sizes[i]) for i, c in enumerate(cols)))
    print("-+-".join("-" * s for s in sizes))
    for row in rows:
        print(" | ".join(cell(row, c).ljust(sizes[i]) for i, c in enumerate(cols)))


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
    amounts: list, opening, closing, balances: list | None = None, tol: float = 0.01
) -> bool:
    """Local mirror of the engine's balance checkers: opening + signed
    amounts must equal closing. Accepts floats or printed tokens (parsed
    with an auto-detected format). When `balances` (the printed running
    balance column, same length as amounts) is given, the first row where
    opening + cumulative amounts diverges from the printed balance is
    reported BY ROW — the exact debugging view a failing table needs.
    ALWAYS run this before calling build_config."""
    global _LAST_RECONCILE_OK
    opening = _coerce_amount(opening, "opening")
    closing = _coerce_amount(closing, "closing")
    amounts = [_coerce_amount(a, "amount") for a in amounts]
    if balances is not None and len(balances) == len(amounts):
        running = opening
        for index, (amount, printed) in enumerate(zip(amounts, balances)):
            running += amount  # always accumulate, even on balance-less rows
            if printed is None or printed == []:
                continue
            printed = _coerce_amount(
                printed[0] if isinstance(printed, list) else printed, "balance"
            )
            if abs(running - printed) > tol:
                print(
                    f"reconcile: row {index} DIVERGES — running {running:,.2f} "
                    f"vs printed balance {printed:,.2f} (amount {amount:+,.2f}); "
                    "check this row's sign/role/region membership"
                )
                _LAST_RECONCILE_OK = False
                return False
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
    if "account_number_patterns" in fields:
        # The engine's account-number matcher silently rejects patterns
        # using groups/alternation — semantically correct regexes still fail
        # ("Missing required fields: account number" for rounds on end).
        # The safe dialect is explicit digit-run shapes: \d{n} plus escaped
        # literals, exactly what pattern_for() emits.
        bad = [
            p for p in fields["account_number_patterns"] if "(" in p or "|" in p
        ]
        if bad:
            raise ValueError(
                f"account_number_patterns {bad} use groups/alternation the "
                "engine's matcher silently rejects. Write the shape with "
                "explicit runs, e.g. pattern_for(printed_value) -> "
                "'\\d{4} \\d{4} \\d{4} \\d{4}'"
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
