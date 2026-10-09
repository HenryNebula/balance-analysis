import re
from datetime import date as calendar_date

import statementledger as pl

KEY = "us__jpmorgan_chase__checking__1"


def parse(frames):
    lines = frames["lines"]

    def text(line):
        return " ".join(item[0] for item in line["items"])

    def beside(label):
        for line in lines:
            for i, item in enumerate(line["items"]):
                if label in item[0]:
                    value = item[0].split(label, 1)[1].strip(" :")
                    if value:
                        return value
                    if i + 1 < len(line["items"]):
                        return line["items"][i + 1][0]
        raise ValueError(f"label not printed: {label}")

    period = next(text(line) for line in lines if "through" in text(line))
    year = int(re.search(r"\b(?:19|20)\d{2}\b", period).group())
    account = beside("Primary Account").strip(" :")
    opening = pl.parse_amount(beside("Beginning Balance"))
    closing_line = next(line for line in lines if "Ending Balance" in text(line))
    closing = next(pl.parse_amount(item[0]) for item in reversed(closing_line["items"])
                   if pl.parse_amount(item[0]) is not None)
    bank = next(text(line) for line in lines if "JPMorgan Chase Bank" in text(line))

    sections = {
        "DEPOSITS AND ADDITIONS": 1,
        "CHECKS PAID": -1,
        "OTHER WITHDRAWALS, FEES & CHARGES": -1,
    }
    transactions, totals = [], []
    active, headers = None, None

    for line in lines:
        raw = text(line)
        if raw in sections:
            active, headers = sections[raw], None
            continue
        if not active:
            continue
        if raw.startswith("Total "):
            value = pl.parse_amount(line["items"][-1][0])
            if active == 1:
                totals.append({"label": raw.split("$")[0].strip(),
                               "kind": "positive", "value": abs(value)})
            active, headers = None, None
            continue
        words = {item[0].upper() for item in line["items"]}
        if "AMOUNT" in words and ("DATE" in words or "CHECK NUMBER" in words):
            headers = sorted(((item[0].upper(), (item[1] + item[2]) / 2)
                              for item in line["items"]), key=lambda col: col[1])
            continue
        if not headers:
            continue
        names = [name for name, _ in headers]
        centers = [center for _, center in headers]
        edges = ([-float("inf")] +
                 [(a + b) / 2 for a, b in zip(centers, centers[1:])] +
                 [float("inf")])
        row = {}
        for item in line["items"]:
            cx = (item[1] + item[2]) / 2
            col = next((names[i] for i in range(len(names))
                        if edges[i] <= cx < edges[i + 1]), names[-1])
            row[col] = (row.get(col, "") + " " + item[0]).strip()
        amount = pl.parse_amount(row.get("AMOUNT", ""), strict_grouping=True)
        if amount is None:
            continue
        date_match = next((re.fullmatch(r"(\d{1,2})/(\d{1,2})", item[0].strip())
                           for item in line["items"]
                           if re.fullmatch(r"\d{1,2}/\d{1,2}", item[0].strip())), None)
        txn_date = (calendar_date(year, int(date_match.group(1)),
                                  int(date_match.group(2))).isoformat()
                    if date_match else None)
        description = " ".join(row.get(name, "") for name in names
                               if name in ("DESCRIPTION", "CHECK NUMBER")).strip()
        transactions.append({"date": txn_date, "description": description,
                             "amount": round(active * amount, 2), "balance": None})

    running = round(opening, 2)
    previous_date = None
    for txn in transactions:
        txn["date"] = txn["date"] or previous_date
        previous_date = txn["date"]
        running = round(running + txn["amount"], 2)
        txn["balance"] = running

    return {"bank_name": bank, "account_number": account,
            "opening_balance": opening, "closing_balance": closing,
            "transactions": transactions, "totals": totals}


if __name__ == "__main__":
    statement = parse(pl.load_frames()[0])
    print(statement)
    pl.reconcile([t["amount"] for t in statement["transactions"]],
                 statement["opening_balance"], statement["closing_balance"],
                 [t["balance"] for t in statement["transactions"]])