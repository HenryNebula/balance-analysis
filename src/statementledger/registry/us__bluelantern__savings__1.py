"""Registered parser for Blue Lantern Savings Bank (synthetic fixture B) —
the README example shape: single signed Amount column, "(45.00)" negatives,
balances printed only where the statement prints them.

The minimal registry artifact: KEY + a pure parse() over frames. Label
values are the item right of the label; columns are binned by the header
row's x-centers; a sticky current_date handles date-less rows; description
wrap lines accumulate until the money arrives. Demo guarded by __name__.
"""

import re

import statementledger as pl

KEY = "us__bluelantern__savings__1"


def parse(frames):
    lines = frames["lines"]
    text = lambda item: item[0].strip()          # noqa: E731 - reads like the grid
    center = lambda item: (item[1] + item[2]) / 2  # noqa: E731

    def value(label):
        for line in lines:
            for i, item in enumerate(line["items"]):
                if text(item).startswith(label):
                    return text(line["items"][i + 1])

    period = value("Statement Period")
    year = int(re.search(r"\b\d{4}\b", period).group())
    opening = pl.parse_amount(value("Beginning Balance"), strict_grouping=True)
    closing = pl.parse_amount(value("Ending Balance"), strict_grouping=True)
    account = value("Account No:")

    header_i = next(i for i, line in enumerate(lines)
                    if all(w in [text(x) for x in line["items"]]
                           for w in ("Date", "Description", "Amount", "Balance")))
    header = lines[header_i]["items"]
    cols = {text(x): center(x) for x in header}
    names = ("Date", "Description", "Amount", "Balance")
    cuts = [(cols[a] + cols[b]) / 2 for a, b in zip(names, names[1:])]

    def column(x):
        return names[sum(x >= cut for cut in cuts)]

    bank = text(lines[0]["items"][0])
    transactions, pending, current_date = [], [], None
    for line in lines[header_i + 1:]:
        if any("End of activity" in text(x) for x in line["items"]):
            break
        amount = balance = None
        for item in line["items"]:
            t, c = text(item), column(center(item))
            if c == "Date":
                current_date = pl.parse_date(t, default_year=year) or current_date
            elif c == "Description":
                pending.append(t)
            elif c == "Amount":
                amount = pl.parse_amount(t, strict_grouping=True)
            elif c == "Balance":
                balance = pl.parse_amount(t, strict_grouping=True)
        if amount is not None:
            transactions.append({"date": current_date,
                                 "description": " ".join(pending).strip(),
                                 "amount": amount, "balance": balance})
            pending = []
    return {"bank_name": bank, "account_number": account,
            "opening_balance": opening, "closing_balance": closing,
            "transactions": transactions}


if __name__ == "__main__":
    statement = parse(pl.load_frames()[0])
    print(statement["bank_name"], statement["account_number"],
          statement["opening_balance"], "->", statement["closing_balance"])
    for txn in statement["transactions"]:
        print(txn)
    pl.reconcile([t["amount"] for t in statement["transactions"]],
                 statement["opening_balance"], statement["closing_balance"],
                 [t["balance"] for t in statement["transactions"]])
