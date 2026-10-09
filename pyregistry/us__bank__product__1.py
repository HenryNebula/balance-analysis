import re
import pyledger as pl

KEY = "us__bank__product__1"

def parse(frames):
    lines = frames["lines"]
    text = lambda item: item[0].strip()
    center = lambda item: (item[1] + item[2]) / 2
    def value(label):
        for line in lines:
            for i, item in enumerate(line["items"]):
                if text(item).startswith(label):
                    return text(line["items"][i + 1])
    period = value("Statement Period:")
    year = int(re.search(r"\b\d{4}\b", period).group())
    opening = pl.parse_amount(value("Opening balance:"), strict_grouping=True)
    closing = pl.parse_amount(value("Closing balance:"), strict_grouping=True)
    account = value("Account number:")
    header_i = next(i for i, line in enumerate(lines)
                    if all(w in [text(x) for x in line["items"]]
                           for w in ("Date", "Description", "Debit", "Credit", "Balance")))
    header = lines[header_i]["items"]
    cols = {text(x): center(x) for x in header}
    names = ("Date", "Description", "Debit", "Credit", "Balance")
    cuts = [(cols[a] + cols[b]) / 2 for a, b in zip(names, names[1:])]
    def column(x):
        return names[sum(x >= cut for cut in cuts)]
    bank = text(lines[0]["items"][0])
    transactions, pending, current_date = [], [], None
    for line in lines[header_i + 1:]:
        if any("Transactions stop here." in text(x) for x in line["items"]):
            break
        debit = credit = balance = None
        for item in line["items"]:
            t, c = text(item), column(center(item))
            if c == "Date":
                current_date = pl.parse_date(t, default_year=year) or current_date
            elif c == "Description":
                pending.append(t)
            elif c == "Debit":
                debit = pl.parse_amount(t, strict_grouping=True)
            elif c == "Credit":
                credit = pl.parse_amount(t, strict_grouping=True)
            elif c == "Balance":
                balance = pl.parse_amount(t, strict_grouping=True)
        if debit is not None or credit is not None:
            amount = -abs(debit) if debit is not None else abs(credit)
            transactions.append({"date": current_date,
                                 "description": " ".join(pending).strip(),
                                 "amount": amount, "balance": balance})
            pending = []
    return {"bank_name": bank, "account_number": account,
            "opening_balance": opening, "closing_balance": closing,
            "transactions": transactions}

if __name__ == "__main__":
    statement = pl.load_frames()[0]
    result = parse(statement)
    print(result)
    pl.reconcile([t["amount"] for t in result["transactions"]],
                 result["opening_balance"], result["closing_balance"],
                 [t["balance"] for t in result["transactions"]])