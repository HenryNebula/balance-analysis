import re
import statementledger as pl

KEY = "hk__silk_road__business__1"


def parse(frames):
    lines = frames["lines"]
    text = lambda line: pl.line_text(line)
    bank = next(text(line) for line in lines
                if "Silk Road Banking (Hong Kong) Limited" in text(line))
    period = next(text(line) for line in lines
                  if re.search(r"\d{2}/\d{2}/\d{4}", text(line)))
    year = int(re.search(r"\d{2}/\d{2}/(\d{4})", period).group(1))

    def amount(value):
        parsed = pl.parse_amount(value)
        if parsed is None:
            match = re.search(r"\d[\d,]*(?:\.\d+)?", value)
            parsed = pl.parse_amount(match.group()) if match else None
        return parsed

    ledgers, active, current = {}, None, None
    in_table, names, edges = False, [], []

    def finish():
        nonlocal current
        if current and current["amount"] is not None:
            active["transactions"].append(current.copy())
        current = None

    for line in lines:
        line_text = text(line)
        items = line["items"]
        account_match = re.search(r"\b\d{3}-\d{6}-\d{3}\b", line_text)
        if account_match and "Account" in line_text:
            finish()
            number = account_match.group()
            if number not in ledgers:
                ledgers[number] = {"account_number": number,
                                   "transactions": [], "totals": []}
            active = ledgers[number]
            in_table = True

        if in_table and "Silk Road Banking (Hong Kong) Limited" in line_text:
            finish()
            in_table = False
            continue

        headers = {item[0] for item in items}
        if {"Date", "Transaction Details", "Deposit", "Withdrawal", "Balance"} <= headers:
            centers = sorted(((item[0], (item[1] + item[2]) / 2)
                              for item in items), key=lambda item: item[1])
            names, xs = zip(*centers)
            edges = ([float("-inf")] +
                     [(a + b) / 2 for a, b in zip(xs, xs[1:])] +
                     [float("inf")])
            continue

        if not active:
            continue

        if "Total No. of Deposits:" in line_text:
            finish()
            in_table = False
            continue

        for label, kind in (("Total Deposit Amount:", "positive"),
                            ("Total Withdrawal Amount:", "negative")):
            if label in line_text:
                label_x = next(x1 for t, x1, _ in items if label in t)
                other = ("Total Withdrawal Amount:" if kind == "positive"
                         else "Total Deposit Amount:")
                next_x = min((x1 for t, x1, _ in items
                              if other in t and x1 > label_x), default=float("inf"))
                values = [amount(t) for t, x1, _ in items if x1 > label_x
                          and x1 < next_x and amount(t) is not None]
                if values:
                    active["totals"].append(
                        {"label": label[:-1], "kind": kind,
                         "value": abs(values[-1])})

        if not in_table or not names:
            continue

        def column(x):
            return next(name for name, lo, hi in zip(names, edges, edges[1:])
                        if lo <= x < hi)

        date = next((d for t, x1, _ in items if x1 < 75
                     if (d := pl.parse_date(t, default_year=year))), None)
        if date:
            finish()
            current = {"date": date, "description": "", "amount": None,
                       "printed_balance": None}
        if current is None:
            continue

        for t, x1, x2 in items:
            name = column((x1 + x2) / 2)
            if name == "Transaction Details" and x1 >= 80:
                current["description"] = (current["description"] + " " + t).strip()
            elif name in ("Deposit", "Withdrawal"):
                value = amount(t)
                if value is not None:
                    current["amount"] = abs(value) if name == "Deposit" else -abs(value)
            elif name == "Balance" and x1 > 480:
                current["printed_balance"] = amount(t)

    finish()
    results = []
    for ledger in ledgers.values():
        rows = ledger["transactions"]
        index = next(i for i, row in enumerate(rows)
                     if row["printed_balance"] is not None)
        opening = round(rows[index]["printed_balance"] -
                        sum(row["amount"] for row in rows[:index + 1]), 2)
        running, previous_date = opening, None
        for row in rows:
            row["date"] = row["date"] or previous_date
            previous_date = row["date"]
            running = round(running + row["amount"], 2)
            row["balance"] = (row["printed_balance"] if row["printed_balance"] is not None
                              else running)
            del row["printed_balance"]
        results.append({"bank_name": bank, "account_number": ledger["account_number"],
                        "opening_balance": opening, "closing_balance": running,
                        "transactions": rows, "totals": ledger["totals"]})
    return results


if __name__ == "__main__":
    statements = parse(pl.load_frames()[0])
    print(statements)
    for statement in statements:
        pl.reconcile([t["amount"] for t in statement["transactions"]],
                     statement["opening_balance"], statement["closing_balance"],
                     [t["balance"] for t in statement["transactions"]])