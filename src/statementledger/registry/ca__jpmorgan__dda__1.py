import re

import statementledger as pl

KEY = "ca__jpmorgan__dda__1"


def parse(frames):
    lines = frames["lines"]
    text = lambda line: pl.line_text(line)

    bank_name = next(text(line) for line in lines
                     if "JPMORGAN CHASE BANK" in text(line))
    account_line = next(line for line in lines if "Account No." in text(line))
    account_number = next(value for item in account_line["items"]
                          for value in re.findall(r"\d{8,}", item[0]))
    year_text = next(re.search(r"\b(?:19|20)\d{2}\b", text(line)).group()
                     for line in lines
                     if "From" in text(line) and re.search(r"\b(?:19|20)\d{2}\b", text(line)))
    year = int(year_text)

    header = next(line for line in lines
                  if {"Number", "Description", "Date", "Amount Due", "Amount", "Due"}
                  <= {item[0] for item in line["items"]})
    labels = {"Number": "number", "Description": "description", "Date": "date",
              "Amount Due": "due", "Amount": "amount", "Due": "balance"}
    centers = [(labels[item[0]], (item[1] + item[2]) / 2)
               for item in header["items"] if item[0] in labels]
    transactions, pending_desc, previous_date = [], "", None
    closing = None

    for line in lines[lines.index(header) + 1:]:
        if "Total Due CAD" in text(line):
            closing = next(pl.parse_amount(item[0]) for item in line["items"]
                           if item[1] > 500 and pl.parse_amount(item[0]) is not None)
            break
        if "Account Analysis" in text(line):
            break
        row = {}
        for item_text, x1, x2 in line["items"]:
            name = min(centers, key=lambda entry: abs((x1 + x2) / 2 - entry[1]))[0]
            row[name] = (row.get(name, "") + " " + item_text).strip()
        date = pl.parse_date(row.get("date", ""), default_year=year)
        amount = pl.parse_amount(row.get("amount", ""), strict_grouping=True)
        if amount is None:
            amount = pl.parse_amount(row.get("due", ""), strict_grouping=True)
        if amount is None:
            pending_desc = (pending_desc + " " + row.get("description", "")).strip()
            continue
        date = date or previous_date
        description = (pending_desc + " " + row.get("description", "")).strip()
        transactions.append({"date": date, "description": description,
                             "amount": amount, "balance": None})
        previous_date, pending_desc = date, ""

    opening = 0.0
    balance = opening
    for txn in transactions:
        balance = round(balance + txn["amount"], 2)
        if txn["balance"] is None:
            txn["balance"] = balance

    return {"bank_name": bank_name, "account_number": account_number,
            "opening_balance": opening, "closing_balance": closing,
            "transactions": transactions}


if __name__ == "__main__":
    statement = parse(pl.load_frames()[0])
    print(statement)
    pl.reconcile([txn["amount"] for txn in statement["transactions"]],
                 statement["opening_balance"], statement["closing_balance"],
                 [txn["balance"] for txn in statement["transactions"]])