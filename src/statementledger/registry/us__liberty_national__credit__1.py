import re
import datetime
import statementledger as pl

KEY = "us__liberty_national__credit__1"


def parse(frames):
    lines = frames["lines"]

    def beside(label):
        for line in lines:
            for text, x1, _ in line["items"]:
                if label in text:
                    right = sorted((x, t) for t, x, _ in line["items"] if x > x1)
                    if right:
                        return right[0][1]
        raise ValueError(f"label not printed: {label}")

    def beside_amount(label):
        for line in lines:
            for text, x1, _ in line["items"]:
                if label in text:
                    values = [pl.parse_amount(t) for t, x, _ in line["items"] if x > x1]
                    values = [v for v in values if v is not None]
                    if values:
                        return values[0]
        raise ValueError(f"amount not printed beside: {label}")

    account = beside("Account Number:")
    opening = beside_amount("Previous Balance")
    closing = beside_amount("New Balance")
    year_text = " ".join(pl.line_text(line) for line in lines)
    year = int(re.search(r"\b(?:19|20)\d{2}\b", year_text).group())
    bank = next(t for line in lines for t, _, _ in line["items"]
                if "Liberty National Bank" in t)

    header = next(line for line in lines
                  if {"Post Date", "Trans Date", "$ Amount"} <=
                  {t for t, _, _ in line["items"]})
    cols = sorted(((t, (a + b) / 2) for t, a, b in header["items"]),
                  key=lambda x: x[1])
    names = [n for n, _ in cols]
    centers = [c for _, c in cols]
    edges = [float("-inf")] + [(a + b) / 2 for a, b in zip(centers, centers[1:])] + [float("inf")]

    def column(cx):
        return next(n for n, lo, hi in zip(names, edges, edges[1:]) if lo <= cx < hi)

    txns, last_date = [], None
    for line in lines[lines.index(header) + 1:]:
        textline = pl.line_text(line)
        if "Totals Year-to-Date" in textline:
            break
        descriptions, dates, amounts = [], [], []
        for text, x1, x2 in line["items"]:
            col = column((x1 + x2) / 2)
            if col == "Post Date":
                dates.append(text)
            elif col == "Merchant Name or Transaction Description":
                descriptions.append(text)
            elif col == "$ Amount":
                value = pl.parse_amount(text)
                if value is not None:
                    amounts.append(value)
        date_token = next((t for t in dates if re.fullmatch(r"\d{2}/\d{2}", t)), None)
        date = (datetime.date(year, int(date_token[:2]), int(date_token[3:])).isoformat()
                if date_token else None)
        desc = " ".join(descriptions).strip()
        amount = next((v for v in amounts if v != 0), None)
        if amount is not None:
            last_date = date or last_date
            txns.append({"date": last_date, "description": desc,
                         "amount": amount, "balance": None})
        elif desc and txns:
            txns[-1]["description"] = (txns[-1]["description"] + " " + desc).strip()

    running = opening
    for txn in txns:
        running = round(running + txn["amount"], 2)
        txn["balance"] = running
    return {"bank_name": bank, "account_number": account,
            "opening_balance": opening, "closing_balance": closing,
            "transactions": txns}


if __name__ == "__main__":
    statement = parse(pl.load_frames()[0])
    print(statement)
    pl.reconcile([t["amount"] for t in statement["transactions"]],
                 statement["opening_balance"], statement["closing_balance"],
                 [t["balance"] for t in statement["transactions"]])