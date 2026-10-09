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


def show(lines: list[dict], n: int = 15, start: int = 0) -> None:
    """Print lines[start:start+n] in layout-dump form, with line indices:

        [Page 0]
        038 ["15 Jan",77,113] ["Transaction 29",149,229] ["1,122.33",282,329]

    Line indices are zero-based offsets into `lines` — the same indices you
    slice with lines[a:b]. Probe the TOP (start=0) and the END
    (start=len(lines)-n; the words that stop the table live there)."""
    page = None
    for index in range(start, min(start + n, len(lines))):
        line = lines[index]
        if line["page"] != page:
            page = line["page"]
            print(f"[Page {page}]")
        items = " ".join(f'["{t}",{x1},{x2}]' for t, x1, x2 in line["items"])
        print(f"{index:03d} {items}")


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
