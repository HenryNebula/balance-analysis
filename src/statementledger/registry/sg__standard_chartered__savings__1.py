import re

import statementledger as pl

KEY = "sg__standard_chartered__savings__1"


def parse(frames):
    lines = frames["lines"]
    header = next(line for line in lines
                  if {"Date", "Description", "Withdrawal (-)", "Deposit (+)", "Balance"}
                  <= {item[0] for item in line["items"]})
    cols = sorted(((text, (x1 + x2) / 2) for text, x1, x2 in header["items"]),
                  key=lambda col: col[1])
    names, centers = zip(*cols)
    edges = [float("-inf")] + [(a + b) / 2 for a, b in zip(centers, centers[1:])] + [float("inf")]

    def column(cx):
        return next(name for name, low, high in zip(names, edges, edges[1:])
                    if low <= cx < high)

    def money(text):
        value = pl.parse_amount(text)
        if value is not None:
            return value
        match = re.search(r"(?<!\w)\(?-?\$?\d[\d,]*\.\d{2}\)?(?:\s*(?:DR|CR))?(?!\w)",
                          text, re.I)
        return pl.parse_amount(match.group()) if match else None

    year = int(re.search(r"\b(?:19|20)\d{2}\b",
                         " ".join(pl.line_text(line) for line in lines)).group())
    account = next(re.search(r"\b\d{4}-\d{4}-\d{4}\b", pl.line_text(line)).group()
                   for line in lines if "Account Number" in pl.line_text(line))
    opening = next(money(text) for line in lines
                   if "Balance Brought Forward" in pl.line_text(line)
                   for text, *_ in line["items"] if money(text) is not None)

    transactions, pending, last_date = [], "", None
    running = opening
    for line in lines[lines.index(header) + 1:]:
        if "Balance Carried Forward" in pl.line_text(line):
            closing = next(pl.parse_amount(text) for text, x1, x2 in line["items"]
                           if column((x1 + x2) / 2) == "Balance"
                           and pl.parse_amount(text) is not None)
            break
        row = {}
        for text, x1, x2 in line["items"]:
            name = column((x1 + x2) / 2)
            row[name] = (row.get(name, "") + " " + text).strip()
        date = pl.parse_date(row.get("Date", ""), default_year=year)
        withdrawal = pl.parse_amount(row.get("Withdrawal (-)", ""))
        deposit = pl.parse_amount(row.get("Deposit (+)", ""))
        amount = -withdrawal if withdrawal is not None else deposit
        if amount is None:
            pending = (pending + " " + row.get("Description", "")).strip()
            continue
        description = (pending + " " + row.get("Description", "")).strip()
        date = date or last_date
        running = round(running + amount, 2)
        balance = pl.parse_amount(row.get("Balance", ""))
        transactions.append({"date": date, "description": description,
                             "amount": amount, "balance": balance if balance is not None else running})
        pending, last_date = "", date

    return {"bank_name": "Standard Chartered Bank", "account_number": account,
            "opening_balance": opening, "closing_balance": closing,
            "transactions": transactions}


if __name__ == "__main__":
    statement = parse(pl.load_frames()[0])
    print(statement)
    pl.reconcile([txn["amount"] for txn in statement["transactions"]],
                 statement["opening_balance"], statement["closing_balance"],
                 [txn["balance"] for txn in statement["transactions"]])