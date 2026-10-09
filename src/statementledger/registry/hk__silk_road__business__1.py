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
    title = next(text(line) for line in lines if "HKD Current Account" in text(line))
    account = re.search(r"\b\d{3}-\d{6}-\d{3}\b", title).group()
    header = next(line for line in lines
                  if {"Date", "Transaction Details", "Deposit", "Withdrawal",
                      "Balance"} <= {item[0] for item in line["items"]})
    centers = sorted(((item[0], (item[1] + item[2]) / 2)
                      for item in header["items"]), key=lambda item: item[1])
    names, xs = zip(*centers)
    edges = [float("-inf")] + [(a + b) / 2 for a, b in zip(xs, xs[1:])] + [float("inf")]

    def column(x):
        return next(name for name, lo, hi in zip(names, edges, edges[1:])
                    if lo <= x < hi)

    def amount(value):
        parsed = pl.parse_amount(value)
        if parsed is None:
            match = re.search(r"\d[\d,]*(?:\.\d+)?", value)
            parsed = pl.parse_amount(match.group()) if match else None
        return parsed

    rows, current = [], None

    def finish():
        if current and current["amount"] is not None:
            rows.append(current.copy())

    for line in lines[lines.index(header) + 1:]:
        if "Total No. of Deposits:" in text(line):
            break
        if any(item[0] == "Date" for item in line["items"]):
            continue
        date = next((d for t, x1, _ in line["items"] if x1 < 75
                     if (d := pl.parse_date(t, default_year=year))), None)
        if date:
            finish()
            current = {"date": date, "description": "", "amount": None,
                       "printed_balance": None}
        if current is None:
            continue
        for t, x1, x2 in line["items"]:
            name = column((x1 + x2) / 2)
            if name == "Transaction Details":
                current["description"] = (current["description"] + " " + t).strip()
            elif name in ("Deposit", "Withdrawal"):
                value = amount(t)
                if value is not None:
                    current["amount"] = abs(value) if name == "Deposit" else -abs(value)
            elif name == "Balance" and x1 > 480:
                current["printed_balance"] = amount(t)
    finish()

    index = next(i for i, row in enumerate(rows)
                 if row["printed_balance"] is not None)
    opening = round(rows[index]["printed_balance"] -
                    sum(row["amount"] for row in rows[:index + 1]), 2)
    running = opening
    for row in rows:
        running = round(running + row["amount"], 2)
        row["balance"] = row["printed_balance"] if row["printed_balance"] is not None else running
        del row["printed_balance"]
    return {"bank_name": bank, "account_number": account,
            "opening_balance": opening, "closing_balance": running,
            "transactions": rows}


if __name__ == "__main__":
    statement = parse(pl.load_frames()[0])
    print(statement)
    pl.reconcile([t["amount"] for t in statement["transactions"]],
                 statement["opening_balance"], statement["closing_balance"],
                 [t["balance"] for t in statement["transactions"]])