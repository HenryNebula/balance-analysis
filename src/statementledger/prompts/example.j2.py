import re

import statementledger as pl

KEY = "us__demo__checking__1"


def parse(frames):
    lines = frames["lines"]

    account = re.search(r"(\d[\d ]+)",
                        pl.anchor_value(lines, "Account No:")).group(1).strip()
    opening = pl.parse_amount(pl.anchor_value(lines, "Beginning Balance"))
    closing = pl.parse_amount(pl.anchor_value(lines, "Ending Balance"))
    year = int(re.search(r"\b(19|20)\d{2}\b",
                         pl.anchor_value(lines, "Statement Period")).group())

    columns, rows = pl.table(lines, stop_terms=("End of activity",))
    transactions, pending_date = [], None
    for row in rows:                       # wrapped descriptions are merged
        date = pl.parse_date(row.get("Date", ""), default_year=year)
        amount = pl.parse_amount(row.get("Amount", ""), strict_grouping=True)
        if amount is None:                 # no money: not a transaction row
            pending_date = date or pending_date
            continue
        transactions.append({
            "date": date or pending_date,
            "description": row.get("Description", ""),
            "amount": amount,
            "balance": pl.parse_amount(row.get("Balance", "")),
        })
        pending_date = None

    return {"bank_name": pl.anchor_value(lines, "Bank"),
            "account_number": account,
            "opening_balance": opening, "closing_balance": closing,
            "transactions": transactions}


if __name__ == "__main__":
    statement = parse(pl.load_frames()[0])
    print(statement["account_number"], statement["opening_balance"],
          "->", statement["closing_balance"])
    for txn in statement["transactions"][:5]:
        print(txn)
    pl.reconcile([t["amount"] for t in statement["transactions"]],
                 statement["opening_balance"], statement["closing_balance"],
                 [t["balance"] for t in statement["transactions"]])
