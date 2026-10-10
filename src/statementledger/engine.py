"""The statementledger engine: token parsers, the canonical statement schema,
and the arithmetic gate.

The idea this engine is built on: a statement is a ledger, and a ledger is
VERIFIABLE — opening + signed amounts must equal closing, printed running
balances must track, and printed section totals must match their parsed sums.
A parse() that gets the ledger right is trusted with zero LLM; one that does
not fails the gate no matter how plausible its output looks.

A registry artifact is a Python module defining

    KEY = "us__bank__product__1"
    def parse(frames) -> dict | list[dict]: ...   # pure; canonical dict(s)

frames is the extracted layout (see statementledger.frames): one statement
dict {"name": str, "lines": [{"page", "y", "items": [[text, x1, x2], ...]},
...]}. parse() derives everything from it at runtime (no transcribed
amounts/dates/descriptions), so a registered module parses FUTURE months of
the same bank with zero LLM. A PDF holding SEVERAL accounts (repeated table
headers, each with its own account number and balances) gets a LIST of
canonical dicts — one per account, each gated on its own arithmetic.

stdlib only; runs both inside the distill sandbox and at reuse time.
"""

from __future__ import annotations

import re

TOL = 0.01

MONTHS = {
    prefix: number
    for number, name in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"], 1)
    for prefix in (name[:3],)
}


def line_text(line: dict) -> str:
    return " ".join(item[0] for item in line["items"])


def render(lines: list[dict], n: int = 40, start: int = 0,
           width: int = 110) -> None:
    """Print lines[start:start+n] as a monospace text wall that preserves
    x positions — each page normalized to `width` columns:

        [Page 0]
        038 | Date   Description            Debit      Credit     Balance
        039 | 01 Feb  STORE 482 PURCHASE    112.33                9,844.21

    Line indices are zero-based offsets into `lines` — the same indices you
    slice with lines[a:b]. Items are placed at a character column derived
    from their x1 (per-page scale), right-to-left so the money/date columns
    on the right win collisions and long descriptions clip with "…" instead
    of mashing into them. Positions are approximations; for exact geometry
    read the raw [text, x1, x2] triples in lines[i]["items"]. Slice, don't
    dump: the interesting regions are the TOP (start=0) and the END
    (start=len(lines)-n; the words that stop the table live there)."""
    page_right: dict[int, int] = {}
    for line in lines:
        for _text, _x1, x2 in line["items"]:
            page_right[line["page"]] = max(page_right.get(line["page"], 0), x2)
    page = None
    for index in range(start, min(start + n, len(lines))):
        line = lines[index]
        if line["page"] != page:
            page = line["page"]
            print(f"[Page {page}]")
        scale = width / max(page_right.get(page, 0) or 600, 1)
        buffer = [" "] * width
        placed: list[int] = []  # start columns of items already placed
        for text, x1, _x2 in reversed(line["items"]):
            col = min(int(round(x1 * scale)), width - 1)
            limit = min(placed) if placed else width  # nearest right neighbor
            room = limit - col - 1  # one blank separator column
            if room <= 0:
                continue  # a rightward item (money wins) covers this span
            shown = text if len(text) <= room else text[: room - 1] + "…"
            buffer[col: col + len(shown)] = list(shown)
            placed.append(col)
        print(f"{index:03d} | " + "".join(buffer).rstrip())


def grep(lines: list[dict], pattern: str, regex: bool = False,
         context: int = 0) -> None:
    """Which lines hold a term — try keywords before guessing labels:

        grep(lines, "balance") ->
            003: Opening balance: $25,000.00 CR
            007: Closing balance: $24,118.09
            2 match(es)

    Substring match on each line's text, case-insensitive (label hunting is
    case-noisy); regex=True switches to a case-insensitive re.search —
    write [A-Z] classes when case matters. context=k prints up to k
    neighboring lines around each hit, "--" between disjoint groups.
    Prints, returns None: a probe tool — parse() wants anchor_value."""
    if regex:
        def matches(text: str) -> bool:
            return re.search(pattern, text, re.I) is not None
    else:
        needle = pattern.lower()

        def matches(text: str) -> bool:
            return needle in text.lower()
    hits = [i for i, line in enumerate(lines) if matches(line_text(line))]
    if not hits:
        print(f"0 match(es) for {pattern!r}")
        return
    wanted: set[int] = set()
    for hit in hits:
        wanted.update(range(max(0, hit - context),
                            min(len(lines), hit + context + 1)))
    previous = None
    for index in sorted(wanted):
        if previous is not None and index > previous + 1:
            print("--")
        print(f"{index:03d}: {line_text(lines[index])}")
        previous = index
    print(f"{len(hits)} match(es)")


def items(frames: dict) -> list[dict]:
    """Flat printed-item records for ONE statement:
    [{text, x1, x2, cx (x-center), y, page, line (index into frames["lines"])}].
    The list you feed straight into pandas:

        df = pd.DataFrame(pl.items(frames))     # one row per printed item
        df[df.line.between(a, b)]               # a table region by line
        df.groupby("line").text.apply(" ".join)  # visual lines as text

    Each frames["lines"][i]["items"] entry is a [text, x1, x2] list; this
    helper flattens them and adds cx for column binning."""
    rows = []
    for index, line in enumerate(frames["lines"]):
        for text, x1, x2 in line["items"]:
            rows.append({"text": text, "x1": x1, "x2": x2, "cx": (x1 + x2) / 2,
                         "y": line["y"], "page": line["page"], "line": index})
    return rows


# --- Token parsers (plain regex; no format-id vocabulary) -----------------------


def parse_amount(token: str | list, strict_grouping: bool = False) -> float | None:
    """Signed value of a printed amount token — handles $, comma grouping,
    CR/DR suffixes (CR +, DR -), trailing and leading minus, and (parenthesised)
    negatives; plain numbers pass through. None when the token holds no digits
    (blank, "—", "Nil"): callers decide whether that means 0 or absent.

    strict_grouping=True additionally rejects tokens whose integer part has
    4+ digits without comma grouping ("3978.53" vs "3,978.53") — statements
    that group thousands print every such value grouped, so an ungrouped run
    is a corrupt/odd token the column's semantics should skip. Use it on a
    column once its samples show grouping.

    Examples:
        parse_amount("1,250.00")      -> 1250.0
        parse_amount("$2,100.55")     -> 2100.55
        parse_amount("500.00 DR")     -> -500.0     (DR negative, CR positive)
        parse_amount("(45.00)")       -> -45.0      (parenthesised negative)
        parse_amount("1,234.56-")     -> -1234.56   (trailing minus)
        parse_amount("3978.53", strict_grouping=True) -> None  (ungrouped)
        parse_amount("")              -> None       (blank/"—"/"Nil")
    None is NOT zero — it means "no readable money here". Rows whose credit
    AND debit cells both parse to None carry no transaction; treat them as
    description lines, and guard your arithmetic against None."""
    if isinstance(token, list):
        token = " ".join(str(t) for t in token)
    text = str(token).strip()
    if not re.search(r"\d", text):
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    negative = negative or text.upper().endswith("DR")
    text = re.sub(r"(?i)(cr|dr)$", "", text).strip()
    negative = negative or text.endswith("-") or text.startswith("-")
    text = text.rstrip("-").lstrip("+-").strip()
    match = re.match(r"^\$?\s*([\d,]+(?:\.\d+)?)$", text)
    if not match:
        return None
    if strict_grouping:
        integer_part = match.group(1).split(".")[0]
        if len(integer_part.replace(",", "")) >= 4 and "," not in integer_part:
            return None
    value = float(match.group(1).replace(",", ""))
    return -value if negative else value


def parse_date(token: str, default_year: int | None = None) -> str | None:
    """ISO "YYYY-MM-DD" reading of a printed date token — handles
    05 Mar / 5 March 2026 / Mar 5, 2026 / 05/03/2026 / 2026-03-05 / 05-03
    (ambiguous pairs are day-first, the statement convention; an impossible
    day-first reading flips to month-first). Yearless tokens take
    default_year; None when the token is not date-shaped.

    Examples:
        parse_date("05 Mar", default_year=2026) -> "2026-03-05"
        parse_date("Mar 5, 2026")               -> "2026-03-05"
        parse_date("05/03/2026")                -> "2026-03-05"
        parse_date("05 Mar")                    -> None  (no year, no default)
        parse_date("Annoying text")             -> None  (not a date)
    Derive default_year from the statement period (e.g. the 4-digit year in
    the value printed beside "Statement Period:"), not from a guess."""
    text = str(token).strip().rstrip(",")
    if re.fullmatch(r"\d{5,}", text):  # bare digit run: an id, not a date
        return None
    parts = re.findall(r"[A-Za-z]+|\d+", text)
    if not parts:
        return None
    alpha = [p for p in parts if p.isalpha()]
    nums = [int(p) for p in parts if p.isdigit()]
    year = None
    year_leads = False
    for index, n in enumerate(nums):
        if 1900 <= n <= 2099:
            year = n
            year_leads = index == 0
    nums = [n for n in nums if not 1900 <= n <= 2099]
    if len(alpha) == 1 and alpha[0].lower()[:3] in MONTHS and nums:
        month = MONTHS[alpha[0].lower()[:3]]
        day = next((n for n in nums if 1 <= n <= 31), None)
        if day is None:
            return None
    elif len(alpha) == 2 and nums:  # Mar 5 (rare doubled form) — take trailing
        return parse_date(f"{alpha[-1]} {nums[-1]}", default_year)
    elif len(nums) >= 2:
        if year_leads:  # 2026-03-05 / 2026/03/05: the pair is month-first
            month, day = nums[0], nums[1]
        else:
            day, month = nums[0], nums[1]  # day-first (statement convention)
            if month > 12 >= day:  # unambiguous month-first when read as day
                day, month = month, day
    else:
        return None
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    if year is None:
        year = default_year
    if year is None:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


# --- The ledger grid (geometry solved once, not per parser) ---------------------


COLUMN_WORD_RE = re.compile(
    r"^(date|posted|description|details|detail|amount|balance|debit|credit|"
    r"withdrawals?|deposits?|type|ref|reference|no\.?|chq|cheque|value)$", re.I)


def _find_header(lines: list[dict]) -> dict | None:
    """The most header-like line: most items that are exactly common column
    words (Date, Description, Amount, Balance, Debit, Credit, ...)."""
    best, best_score = None, 0
    for line in lines:
        score = sum(1 for item in line["items"]
                    if COLUMN_WORD_RE.match(item[0].strip()))
        if score >= 2 and score > best_score:
            best, best_score = line, score
    return best


def _page_furniture(lines: list[dict]) -> set[int]:
    """Indexes of repeated page furniture — lines whose full text recurs at
    the same y on two or more pages (running headers/footers). The grid
    drops these or they masquerade as continuation rows and pollute the
    description/date columns."""
    seen: dict[tuple[str, int], set[int]] = {}
    by_key: dict[tuple[str, int], list[int]] = {}
    for index, line in enumerate(lines):
        key = (line_text(line), line["y"])
        seen.setdefault(key, set()).add(line["page"])
        by_key.setdefault(key, []).append(index)
    return {index for key, pages in seen.items() if len(pages) >= 2
            for index in by_key[key]}


def _has_money(cell: str) -> bool:
    return any(parse_amount(token) is not None for token in cell.split())


def _has_date(cell: str) -> bool:
    return any(parse_date(token) is not None for token in cell.split())


def table(lines: list[dict], header: dict | None = None, start: int | None = None,
          stop: int | None = None, stop_terms: tuple[str, ...] | list[str] = (),
          sections: dict[str, str] | None = None,
          merge_continuations: bool = True) -> tuple[list[str], list[dict]]:
    """Materialize the ledger grid: (columns, rows) — the geometry solver.

        columns, rows = table(lines, stop_terms=("Transactions stop",))
        -> (["Date", "Description", "Debit", "Credit", "Balance"],
            [{"Date": "01 Feb", "Description": "STORE 482 PURCHASE",
              "Debit": "112.33", "Credit": "", "Balance": "9,844.21",
              "_line": 8}, ...])

    - header: the header line (auto-detected when omitted; pass lines[i]
      yourself when the detector misses — a line whose items are exactly
      common column words)
    - start/stop: data line range (default: after the header, to the first
      line containing any stop term, else end of lines)
    - sections: {label: marker} — every row carries "_section" set to the
      label of the last marker line at or above it (sectioned statements:
      withdrawals/deposits blocks over one shared column set)
    - merge_continuations: rows with no date and no money are wrapped
      descriptions — they merge into the NEXT row (printers set the wrap
      line above the money row); a trailing orphan stays its own row.
      Banks that wrap BELOW the money row differ: audit with "_line".

    Column boundaries are the midpoints between adjacent header item
    centers, edge columns open-ended — the grid a human sees. Page
    furniture is dropped; every row carries "_line" (its index into lines)
    so merges stay auditable. Never rebuild this grid by hand in parse()."""
    header = header or _find_header(lines)
    if header is None:
        raise ValueError(
            "no header row found — pass header=lines[i] for the line whose "
            "items are the column words (find it with grep(lines, 'date'))")
    heads = sorted(((text.strip(), (x1 + x2) / 2)
                    for text, x1, x2 in header["items"]), key=lambda h: h[1])
    columns = [text for text, _ in heads]
    centers = [center for _, center in heads]
    bounds = [(float("-inf") if i == 0 else (centers[i - 1] + centers[i]) / 2,
               float("inf") if i == len(centers) - 1
               else (centers[i + 1] + centers[i]) / 2)
              for i in range(len(centers))]

    first = start if start is not None else lines.index(header) + 1
    last = stop if stop is not None else len(lines)
    if stop_terms and stop is None:
        for index in range(first, len(lines)):
            if any(term in line_text(lines[index]) for term in stop_terms):
                last = min(last, index)
                break

    section_of: str | None = None
    furniture = _page_furniture(lines)
    rows: list[dict] = []
    for index in range(first, min(last, len(lines))):
        if index in furniture:
            continue
        text = line_text(lines[index])
        if sections:
            for label, marker in sections.items():
                if marker in text:
                    section_of = label
                    break
        row = {column: "" for column in columns}
        for item_text, x1, x2 in lines[index]["items"]:
            center = (x1 + x2) / 2
            for column, (low, high) in zip(columns, bounds):
                if low <= center < high:
                    row[column] = (row[column] + " " + item_text).strip()
                    break
        row["_line"] = index
        if sections:
            row["_section"] = section_of
        if not any(row[column] for column in columns):
            continue  # blank line
        rows.append(row)

    if not merge_continuations:
        return columns, rows
    desc = next((c for c in columns
                 if "desc" in c.lower() or "detail" in c.lower()), None)
    if desc is None:
        return columns, rows

    def anchored(row: dict) -> bool:
        return any(_has_money(row[c]) or _has_date(row[c]) for c in columns)

    merged: list[dict] = []
    pending: dict | None = None  # wrap lines awaiting their money row
    for row in rows:
        if not anchored(row) and row[desc]:
            if pending is None:  # consecutive wraps chain onto the first
                pending = dict(row)
            else:
                pending[desc] = (pending[desc] + " " + row[desc]).strip()
                for column in columns:  # keep any stray non-description content
                    if column != desc and row[column]:
                        pending[column] = (pending[column] + " "
                                           + row[column]).strip()
            continue
        if pending is not None:
            row[desc] = (pending[desc] + " " + row[desc]).strip()
            for column in columns:
                if column != desc and pending.get(column):
                    row[column] = (pending[column] + " " + row[column]).strip()
            pending = None
        merged.append(row)
    if pending is not None:  # trailing orphan: no money row ever followed
        merged.append(pending)
    return columns, merged


def render_table(columns: list[str], rows: list[dict],
                 width: int = 28) -> None:
    """Print the materialized grid as an aligned table — read it like a page:

        Date   | Description           | Debit    | Balance
        ------+-----------------------+----------+---------
        01 Feb | STORE 482 PURCHASE    | 112.33   | 9,844.21

    Empty columns are omitted; cells wider than `width` chars are capped
    with "…". Pair with table(): columns, rows = table(...), then
    render_table(columns, rows)."""
    def cell(row: dict, column: str) -> str:
        text = str(row.get(column, ""))
        return (text[: width - 1] + "…") if len(text) > width else text

    cols = [c for c in columns if any(str(r.get(c, "")).strip() for r in rows)] + [
        c for c in ("_section",) if any(r.get("_section") for r in rows)]
    sizes = [max(len(c), *(len(cell(r, c)) for r in rows)) if rows else len(c)
             for c in cols]
    print(" | ".join(c.ljust(sizes[i]) for i, c in enumerate(cols)))
    print("-+-".join("-" * s for s in sizes))
    for row in rows:
        print(" | ".join(cell(row, c).ljust(sizes[i]) for i, c in enumerate(cols)))


def _values_on_line(line: dict, term: str) -> list[str] | None:
    """Item texts strictly right of the item containing `term`, or None."""
    for text, x1, _x2 in line["items"]:
        if term.lower() in text.lower():
            right = [t for t, a, _b in line["items"] if a > x1]
            if right:
                return right
    return None


def anchor_value(lines: list[dict], term: str, index: int = 0) -> str:
    """The printed value beside a label — the beside() every parser needs:

        anchor_value(lines, "Account No:") -> "0123 4567 8901"

    The index-th (default first) line whose text contains `term`
    (case-insensitive) contributes every item RIGHT of the label item —
    same-line rightward, the y-alignment the printer itself used. Raises
    ValueError quoting the printed line when the label carries nothing
    beside it, and saying the term appears nowhere otherwise: grep()
    first when unsure what the label actually says."""
    seen = 0
    quoted = None
    for line in lines:
        values = _values_on_line(line, term)
        if values is None:
            if quoted is None and term.lower() in line_text(line).lower():
                quoted = line_text(line)
            continue
        if seen == index:
            return " ".join(values)
        seen += 1
    if quoted is not None:
        raise ValueError(
            f"{term!r} is printed with nothing beside it: {quoted!r}")
    raise ValueError(f"label not printed anywhere: {term!r}")


def regions(lines: list[dict], window: int = 6) -> list[dict]:
    """Indicative layout map — where the table/form/prose blocks sit:

        regions(lines) ->
            lines 000-005  FORM    5/6 label: value (e.g. "Opening balance: ...")
            lines 007-040  TABLE   columns x≈96,180,310,384 · 79% money · header at 007
            lines 041-042  PROSE   7+ words/line, no money

    Heuristic, NOT truth: windows of consecutive non-furniture lines vote on
    money/date density, recurrent x-centers (the column signature), label:
    value shape, and word count; majority wins per line. Bare digit runs
    (reference numbers) read as money — the ratios are indicative. Use the
    map to aim render()/grep()/table() at the right slice, never as a
    verdict. Returns the region dicts and prints the report."""
    furniture = _page_furniture(lines)
    active = [i for i in range(len(lines)) if i not in furniture]
    if not active:
        print("no non-furniture lines")
        return []
    need = max(3, (window + 1) // 2)

    stats = []
    for i in active:
        tokens = line_text(lines[i]).split()
        first = lines[i]["items"][0][0].rstrip() if lines[i]["items"] else ""
        stats.append({
            "money": any(parse_amount(t) is not None for t in tokens),
            "dated": any(parse_date(t) is not None for t in tokens),
            "colon": first.endswith(":") and len(lines[i]["items"]) >= 2,
            "words": len(tokens),
            "bins": {int(((x1 + x2) / 2) // 16) for _t, x1, x2 in lines[i]["items"]},
        })

    def window_class(chunk: list[dict]) -> tuple[str, dict]:
        counts: dict[int, int] = {}
        for stat in chunk:
            for b in stat["bins"]:
                counts[b] = counts.get(b, 0) + 1
        recurrent = {b for b, n in counts.items() if n >= need}
        evidence = {
            "money": round(sum(s["money"] for s in chunk) / len(chunk)),
            "dated": round(sum(s["dated"] for s in chunk) / len(chunk)),
            "colon": round(sum(s["colon"] for s in chunk) / len(chunk)),
            "columns": sorted(recurrent),
            "words": sum(s["words"] for s in chunk) / len(chunk),
        }
        if len(recurrent) >= 3 and (evidence["money"] >= 0.25
                                    or evidence["dated"] >= 0.5):
            kind = "table"
        elif evidence["colon"] >= 0.5:
            kind = "form"
        elif evidence["words"] >= 7 and evidence["money"] <= 0.2 \
                and len(recurrent) <= 1:
            kind = "prose"
        else:
            kind = "mixed"
        return kind, evidence

    votes: dict[int, dict[str, int]] = {i: {} for i in active}
    best: dict[int, dict] = {i: {} for i in active}
    for offset in range(len(active)):
        chunk = stats[offset: offset + window]
        if not chunk:
            break
        kind, evidence = window_class(chunk)
        for stat_index in range(offset, min(offset + window, len(active))):
            line_index = active[stat_index]
            votes[line_index][kind] = votes[line_index].get(kind, 0) + 1
            if len(evidence["columns"]) > len(best[line_index].get("columns", ())):
                best[line_index] = evidence

    classified = []
    precedence = ("table", "form", "prose", "mixed")  # ties go specific-first
    for i in active:
        kind = max(votes[i].items(),
                   key=lambda kv: (kv[1], -precedence.index(kv[0])))[0]
        classified.append((i, kind))

    merged: list[dict] = []
    for i, kind in classified:
        if merged and (merged[-1]["kind"] == kind):
            merged[-1]["stop"] = i
        else:
            merged.append({"start": i, "stop": i, "kind": kind})
    # single-line runs absorb into the previous region (boundary noise)
    for region_index in range(len(merged) - 1, 0, -1):
        if merged[region_index]["start"] == merged[region_index]["stop"]:
            merged[region_index - 1]["stop"] = merged[region_index]["stop"]
            merged.pop(region_index)

    for region in merged:
        region.update(best[region["start"]])
        region["sample"] = line_text(lines[region["start"]])
        if region["kind"] == "table":
            header = _find_header(lines[region["start"]: region["stop"] + 1])
            region["header"] = (region["start"] + lines[region["start"]:
                                region["stop"] + 1].index(header)) if header else None

    for region in merged:
        label = region["kind"].upper()
        detail = ""
        if region["kind"] == "form":
            detail = f'{region["colon"]:.0%} label: value (e.g. {region["sample"][:40]!r})'
        elif region["kind"] == "table":
            xs = ", ".join(str(b * 16 + 8) for b in region["columns"][:6])
            detail = (f'columns x≈{xs} · {region["money"]:.0%} money'
                      + (f' · header at {region["header"]}'
                         if region.get("header") is not None else ""))
        elif region["kind"] == "prose":
            detail = f'{region["words"]:.0f}+ words/line, no money'
        print(f'lines {region["start"]:03d}-{region["stop"]:03d}  '
              f'{label:<6}  {detail}')
    if furniture:
        print(f"furniture: {sorted(furniture)} — repeated at the same y "
              "on 2+ pages")
    return merged


def reconcile(amounts: list, opening, closing, balances: list | None = None,
              tol: float = TOL) -> bool:
    """The ledger check: opening + signed amounts == closing (amounts accept
    floats or printed tokens). With `balances` (printed running balance per
    row, aligned with amounts, None where the row prints none), the first
    divergent ROW is named — the exact debugging view a failing table needs:

        pl.reconcile(amounts, opening, closing,
                     [t["balance"] for t in transactions])
        # reconcile: OK (opening 50,000.00 -> closing 11,663.82)
        # reconcile: row 29 DIVERGES — running 73,978.53 vs printed
        #   3,978.53 (amount +0.53)

    A FAIL names the problem class: a drift of exactly one amount means a
    wrong sign or a mis-binned column; a mid-table divergence with a large
    gap means a dropped/duplicated row (check the stop term and the
    description-wrap merging). Unreadable opening/closing tokens raise
    ValueError — that means your label anchor is wrong, not the math."""
    def num(value, what):
        if isinstance(value, (int, float)):
            return float(value)
        parsed = parse_amount(value)
        if parsed is None:
            raise ValueError(f"{what} {value!r} is not a readable amount")
        return parsed

    opening = num(opening, "opening")
    closing = num(closing, "closing")
    values = [num(a, "amount") for a in amounts]
    if balances is not None and len(balances) == len(values):
        running = opening
        for index, (amount, printed) in enumerate(zip(values, balances)):
            running += amount  # always accumulate, even on balance-less rows
            if printed in (None, "", []):
                continue
            expected = num(printed[0] if isinstance(printed, list) else printed,
                           "balance")
            if abs(running - expected) > tol:
                print(f"reconcile: row {index} DIVERGES — running {running:,.2f} "
                      f"vs printed {expected:,.2f} (amount {amount:+,.2f})")
                return False
    running = opening + sum(values)
    drift = round(running - closing, 2)
    if abs(drift) <= tol:
        print(f"reconcile: OK (opening {opening:,.2f} -> closing {closing:,.2f})")
        return True
    print(f"reconcile: FAIL — opening {opening:,.2f} + amounts = {running:,.2f}, "
          f"closing printed {closing:,.2f} (drift {drift:+,.2f}; check for a "
          "dropped row, a wrong sign, or a mis-merged column)")
    return False


# --- The canonical statement + verification -------------------------------------

CANONICAL_FIELDS = ("bank_name", "account_number", "opening_balance",
                    "closing_balance", "transactions")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def ledgers_of(result) -> list:
    """A parse() result as a list of ledger dicts: single-account parsers
    return one dict, multi-account parsers (one PDF, several accounts, each
    with its own balances) return a LIST of dicts, one per account."""
    return result if isinstance(result, list) else [result]


def validate(stmt) -> list[str]:
    """Schema errors of a parse() result (empty list = valid): one canonical
    dict, or a LIST of them for a multi-account PDF. Every message names the
    offending field/row and the fix — these strings are exactly what the
    distill gate feeds back to the drafter."""
    canonical = ("{bank_name, account_number, opening_balance, "
                 "closing_balance, transactions}")
    if isinstance(stmt, list):
        if not stmt:
            return ["parse() returned an empty list — a multi-account PDF "
                    "needs one canonical dict PER account"]
        errors = []
        for index, ledger in enumerate(stmt):
            label = (ledger.get("account_number", "?")
                     if isinstance(ledger, dict) else "?")
            errors.extend(f"ledger {index} ({label}): {error}"
                          for error in validate(ledger))
        return errors
    if not isinstance(stmt, dict):
        return [f"parse() returned {type(stmt).__name__}, not the canonical "
                f"dict {canonical} (or a list of them, one per account)"]
    errors = []
    for field in CANONICAL_FIELDS:
        if field not in stmt:
            errors.append(f"missing field {field!r} — parse() must return the "
                          f"canonical dict {canonical}")
    if errors:
        return errors
    for field in ("bank_name", "account_number"):
        if not isinstance(stmt[field], str) or not stmt[field].strip():
            errors.append(
                f"{field} must be a non-empty string, got {stmt[field]!r} — "
                "read it from the statement header (the value printed right "
                "of the label, e.g. beside 'Account number:')")
    for field in ("opening_balance", "closing_balance"):
        if not isinstance(stmt[field], (int, float)):
            errors.append(
                f"{field} must be a number, got {stmt[field]!r} — it is the "
                "value printed beside its label; pl.parse_amount() returning "
                "None means the anchor label or the token is wrong")
    txns = stmt["transactions"]
    if not isinstance(txns, list) or not txns:
        return errors + [
            "transactions is empty — no row bound money. Check that (a) the "
            "header row was found (its items are the column names), (b) the "
            "table region is right (the stop term must be the words printed "
            "AFTER the last transaction), and (c) amount cells parse "
            "(pl.parse_amount returns None for unreadable tokens)"]
    totals = stmt.get("totals", [])
    if totals is not None:
        if not isinstance(totals, list):
            errors.append('totals must be a list of {"label", "kind", '
                          '"value"} checks or absent')
        else:
            for index, total in enumerate(totals):
                if (not isinstance(total, dict)
                        or not isinstance(total.get("label"), str)
                        or total.get("kind") not in TOTAL_KINDS
                        or not isinstance(total.get("value"), (int, float))):
                    errors.append(
                        f"totals[{index}] must be a dict {{label: str, kind: "
                        f"'positive'|'negative'|'all', value: number}} — the "
                        "printed section totals the statement itself states "
                        "(e.g. 'Total deposits and other additions'), bound "
                        "with pl.parse_amount at runtime")

    def where(index: int, txn: dict) -> str:
        label = txn.get("description") or txn.get("date") or ""
        return f"transactions[{index}] ({str(label)[:40]!r})"

    for index, txn in enumerate(txns):
        if not isinstance(txn, dict):
            errors.append(f"transactions[{index}] is a "
                          f"{type(txn).__name__}, not a dict")
            continue
        if not isinstance(txn.get("description", ""), str):
            errors.append(f"{where(index, txn)}: description must be a string "
                          "(join the description column's items)")
        amount = txn.get("amount")
        if not isinstance(amount, (int, float)):
            errors.append(
                f"{where(index, txn)}: amount must be a number, got "
                f"{amount!r} — pl.parse_amount() returns None for unreadable "
                "tokens; a row with no readable credit AND no debit is a "
                "description-wrap line, not a transaction")
        date = txn.get("date")
        if date is not None and not (isinstance(date, str) and ISO_RE.match(date)):
            errors.append(
                f"{where(index, txn)}: date must be an ISO 'YYYY-MM-DD' "
                f"string from pl.parse_date() or None, got {date!r}")
        balance = txn.get("balance")
        if balance is not None and not isinstance(balance, (int, float)):
            errors.append(
                f"{where(index, txn)}: balance must be a number or None, "
                f"got {balance!r}")
    return errors


def hint_for(message: str) -> str:
    """A remedial hint for crash shapes drafters hit repeatedly, keyed off
    the error text (works on tracebacks as well as live exceptions). Empty
    string when the shape is unknown."""
    if "KeyError" in message and any(
            f"'{key}'" in message or f'"{key}"' in message
            for key in ("lines", "items", "name", "page", "y")):
        return ("frames is ONE statement dict: {'name':…, 'lines': [{'page':"
                "…, 'y':…, 'items': [[text,x1,x2],…]},…]} — the keys live on "
                "the statement and its lines, and items are LISTS")
    if "string indices must be integers" in message:
        return ("you subscripted a str with a string — you are iterating a "
                "dict (that yields its key strings) or holding a text token. "
                "frames['lines'] is the line list; each line has "
                "['items'] = [[text, x1, x2], …]")
    if "list indices must be integers" in message:
        return ("you subscripted a list with a string — line['items'] entries "
                "are LISTS [text, x1, x2]: item[0] text, item[1] x1, "
                "item[2] x2. Dict keys ('lines', 'items') live on the "
                "statement/line dicts")
    if "NoneType" in message:
        return ("a None leaked into your logic — pl.parse_amount()/"
                "pl.parse_date() return None for unreadable tokens (blank, "
                "'—', non-money text). Check for None before arithmetic; a "
                "row whose money cells are all None is a description "
                "line, not a transaction")
    return ""


def _crash_hint(error: BaseException) -> str:
    """hint_for() over a live exception."""
    return hint_for(f"{type(error).__name__}: {error}")


TOTAL_KINDS = ("positive", "negative", "all")


def reconcile_statement(stmt, tol: float = TOL) -> str | None:
    """Ledger check on a parsed statement (a dict, or a list of them for a
    multi-account PDF): None when every ledger reconciles, else the first
    divergence (closing drift, the row whose printed balance breaks
    the running sum, or a printed section total that misses its parsed sum).
    Interpreting the message:
    - one row diverges, the rest track -> that row's sign or column binding
      is wrong (credit/debit swapped, or an amount binned to the wrong
      column)
    - divergence equals one printed amount -> a row was dropped or doubly
      counted: check the stop term and description-wrap merging
    - everything diverges from row 0 -> opening_balance is wrong (the anchor
      label or its token)
    - a totals[...] check fails while end-to-end passes -> a dropped
      offsetting PAIR, a double sign flip, or amounts reconstructed from
      the balance chain instead of read from the amount column — errors
      end-to-end reconcile mathematically cannot see
    """
    if isinstance(stmt, list):
        for index, ledger in enumerate(stmt):
            failure = reconcile_statement(ledger, tol)
            if failure:
                label = (ledger.get("account_number", "?")
                         if isinstance(ledger, dict) else "?")
                return f"ledger {index} ({label}): {failure}"
        return None
    errors = validate(stmt)
    if errors:
        return errors[0]
    opening = float(stmt["opening_balance"])
    running = opening
    for index, txn in enumerate(stmt["transactions"]):
        running += float(txn["amount"])
        printed = txn.get("balance")
        if printed is not None and abs(running - float(printed)) > tol:
            return (f"row {index} ({txn['description'][:40]!r}): running "
                    f"{running:,.2f} vs printed balance {printed:,.2f} "
                    f"(amount {txn['amount']:+,.2f})")
    drift = round(running - float(stmt["closing_balance"]), 2)
    if abs(drift) > tol:
        return (f"opening {opening:,.2f} + amounts = {running:,.2f} but "
                f"closing printed {stmt['closing_balance']:,.2f} "
                f"(drift {drift:+,.2f}; suspect a dropped or duplicated row, "
                "a wrong sign, or a mis-binned column)")
    amounts = [float(t["amount"]) for t in stmt["transactions"]]
    sums = {
        "positive": sum(a for a in amounts if a > 0),
        "negative": sum(-a for a in amounts if a < 0),
        "all": sum(amounts),
    }
    for total in stmt.get("totals", []):
        expected = sums[total["kind"]]
        if abs(expected - float(total["value"])) > tol:
            return (f"totals check {total['label']!r}: parsed "
                    f"{total['kind']} amounts sum to {expected:,.2f} but the "
                    f"statement prints {float(total['value']):,.2f} (drift "
                    f"{expected - float(total['value']):+,.2f}; suspect a "
                    "dropped offsetting pair, a double sign flip, or amounts "
                    "derived from balances instead of the amount column)")
    return None


def evaluate(parse_fn, statements: list[dict], key: str | None = None,
             expect_ledgers: dict[str, int] | None = None) -> dict:
    """Run parse_fn over every statement; the verdict the harness consumes:
    {key, stage, results: [{name, ok, rows, ledgers, error}], clean, verified}.
    The error strings are the drafter's feedback: crashes carry a remedial
    hint for known shapes (see _crash_hint), schema violations name the row
    and the fix, and reconcile failures name the divergent row.
    expect_ledgers (optional): per-statement ledger counts {statement name:
    count} pinned from a PREVIOUSLY VERIFIED version — maintenance must keep
    returning the same count per statement. Arithmetic that reconciles over
    a different number of ledgers is partial (or split) coverage the gate
    cannot otherwise see, so it fails with a coverage error instead of
    verifying. Statements absent from the map carry no expectation."""
    results = []
    clean = 0
    for frames in statements:
        entry = {"name": frames["name"], "ok": False, "rows": 0, "error": ""}
        expected = (expect_ledgers or {}).get(frames["name"])
        try:
            stmt = parse_fn(frames)
        except Exception as error:  # noqa: BLE001 - the verdict IS the catcher
            hint = _crash_hint(error)
            entry["error"] = f"{type(error).__name__}: {error}"[:400]
            if hint:
                entry["error"] += f" | HINT: {hint}"
            results.append(entry)
            continue
        schema_errors = validate(stmt)
        if schema_errors:
            entry["error"] = "schema: " + "; ".join(schema_errors[:3])
        else:
            failure = reconcile_statement(stmt)
            if failure:
                entry["error"] = f"reconcile: {failure}"
            else:
                ledgers = ledgers_of(stmt)
                entry["rows"] = sum(len(l["transactions"]) for l in ledgers)
                entry["ledgers"] = len(ledgers)
                if expected is not None and len(ledgers) != expected:
                    entry["error"] = (
                        f"coverage: parse() returned {len(ledgers)} ledger(s) "
                        f"but the registered parser returned {expected} for "
                        "this statement — keep every account covered (a LIST "
                        "with one canonical dict PER account); arithmetic that "
                        "reconciles over a subset of the accounts is partial "
                        "coverage")
                else:
                    entry["ok"] = True
                    clean += 1
        results.append(entry)
    return {"key": key, "stage": "parse", "results": results, "clean": clean,
            "verified": clean == len(statements) and clean > 0}


class VerificationError(Exception):
    """A registered parser failed on a statement — the module does not fit it."""
