"""Canned pyledger parser for Gravy Toast Bank (test1.pdf).

The artifact a --py distill run should converge to: KEY + a pure parse()
turning frames into the canonical statement dict — no config DSL, no format
ids, plain Python geometry. Demo guarded by __name__ so importing the module
(as the gate and the registry do) has no side effects.
"""

import re

import pyledger as pl

KEY = "us__gravytoastbank__statement__3"


def parse(frames):
    lines = frames["lines"]

    def beside(label):
        for line in lines:
            for text, x1, _x2 in line["items"]:
                if label in text:
                    values = [t for t, a, _b in line["items"] if a > x1]
                    if values:
                        return " ".join(values)
        raise ValueError(f"label not printed: {label}")

    account = beside("Account number:")
    opening = pl.parse_amount(beside("Opening balance:"))
    closing = pl.parse_amount(beside("Closing balance:"))
    year = int(re.search(r"\b(19|20)\d{2}\b", beside("Statement Period:")).group())

    header = next(line for line in lines if {"Date", "Description", "Balance"}
                  <= {item[0] for item in line["items"]})
    cols = sorted(((t, (x1 + x2) / 2) for t, x1, x2 in header["items"]),
                  key=lambda c: c[1])
    names = [name for name, _ in cols]
    centers = [center for _, center in cols]
    edges = ([float("-inf")] + [(a + b) / 2 for a, b in zip(centers, centers[1:])]
             + [float("inf")])

    def column_of(cx):
        for name, low, high in zip(names, edges, edges[1:]):
            if low <= cx < high:
                return name
        return names[-1]

    # running headers/footers: identical text at the same y on >= 2 pages
    pages_at = {}
    for line in lines:
        pages_at.setdefault((pl.line_text(line), line["y"]), set()).add(line["page"])
    furniture = {key_ for key_, pages in pages_at.items() if len(pages) >= 2}

    first = lines.index(header) + 1
    last = next(i for i, line in enumerate(lines)
                if "Transactions stop here." in pl.line_text(line))

    transactions = []
    pending_date, pending_desc = None, ""
    for line in lines[first:last]:
        if (pl.line_text(line), line["y"]) in furniture:
            continue
        row = {}
        for text, x1, x2 in line["items"]:
            name = column_of((x1 + x2) / 2)
            row[name] = (row.get(name, "") + " " + text).strip()
        if not any(row.values()):
            continue
        date = pl.parse_date(row.get("Date", ""), default_year=year)
        credit = pl.parse_amount(row.get("Credit", ""), strict_grouping=True)
        debit = pl.parse_amount(row.get("Debit", ""), strict_grouping=True)
        if credit is None and debit is None:
            # description-first wrapping: the money prints on a later line of
            # the same logical row — accumulate and emit when it arrives
            if date is not None:
                pending_date = date
            if row.get("Description"):
                pending_desc = (pending_desc + " " + row["Description"]).strip()
            continue
        transactions.append({
            "date": date or pending_date,
            "description": (pending_desc + " " + row.get("Description", "")).strip(),
            "amount": round((credit or 0.0) - (debit or 0.0), 2),
            "balance": pl.parse_amount(row.get("Balance", ""), strict_grouping=True),
        })
        pending_date, pending_desc = None, ""

    # presentation semantics (same as the rules engine): a row without its
    # own date inherits the previous row's; a row without a printed balance
    # gets the running computed one. Printed values stay as bound — the
    # reconcile gate checks those against the running sum.
    running = opening
    last_date = None
    for txn in transactions:
        running = round(running + txn["amount"], 2)
        if txn["date"] is None:
            txn["date"] = last_date
        else:
            last_date = txn["date"]
        if txn["balance"] is None:
            txn["balance"] = running

    return {
        "bank_name": "Gravy Toast Bank",
        "account_number": account,
        "opening_balance": opening,
        "closing_balance": closing,
        "transactions": transactions,
    }


if __name__ == "__main__":
    statement = parse(pl.load_frames()[0])
    print(statement["bank_name"], statement["account_number"],
          statement["opening_balance"], "->", statement["closing_balance"])
    for txn in statement["transactions"][:6]:
        print(txn)
    print(f"... {len(statement['transactions'])} transactions")
    pl.reconcile([t["amount"] for t in statement["transactions"]],
                 statement["opening_balance"], statement["closing_balance"],
                 [t["balance"] for t in statement["transactions"]])
