import re

import statementledger as pl

KEY = "nl__continentaltrust__personal__1"


def parse(frames):
    lines = frames["lines"]

    def beside(term):
        for i, line in enumerate(lines):
            for text, x1, _ in line["items"]:
                if term in text:
                    for candidate in [line] + lines[i + 1:]:
                        values = [t for t, x, _ in candidate["items"] if x >= x1 and t != text]
                        if values:
                            return " ".join(values)
        raise ValueError(f"label not printed: {term}")

    def money(text):
        match = re.search(r"-?\d[\d.]*,\d{2}", text)
        return pl.parse_amount(match.group().replace(".", "").replace(",", ".")) if match else None

    account = next(re.search(r"IBAN:\s*(\S+)", text).group(1)
                   for line in lines for text, _, _ in line["items"] if "IBAN:" in text)
    bank = beside("Bank information")
    balance_lines = [line for line in lines
                     if any("Balance as of" in t for t, _, _ in line["items"])]
    opening, closing = (money(next(t for t, x, _ in line["items"] if x > 134))
                        for line in balance_lines)
    year = int(re.search(r"\b(?:19|20)\d{2}\b",
                         " ".join(t for t, _, _ in balance_lines[0]["items"])).group())
    header = next(line for line in lines
                  if {"Date", "Amount"} <= {t for t, _, _ in line["items"]})
    cols = sorted(((t, (a + b) / 2) for t, a, b in header["items"]), key=lambda c: c[1])
    names, centers = zip(*cols)
    edges = [float("-inf")] + [(a + b) / 2 for a, b in zip(centers, centers[1:])] + [float("inf")]

    def column(cx):
        return next(n for n, lo, hi in zip(names, edges, edges[1:]) if lo <= cx < hi)

    transactions, previous_date, pending = [], None, ""
    for line in lines[lines.index(header) + 1:]:
        row = {}
        for text, a, b in line["items"]:
            name = column((a + b) / 2)
            row[name] = (row.get(name, "") + " " + text).strip()
        date_text = re.sub("mei", "May", row.get("Date", ""), flags=re.I)
        date = pl.parse_date(date_text, default_year=year) or previous_date
        amount = money(row.get("Amount", ""))
        if amount is None:
            if row.get("Description", ""):
                pending = (pending + " " + row["Description"]).strip()
            continue
        description = " ".join(x for x in (pending, row.get("Counterparty", ""),
                                            row.get("Description", ""))
                               if x and not x.startswith("NL"))
        transactions.append({"date": date, "description": description,
                             "amount": amount, "balance": None})
        previous_date, pending = date, ""

    running = opening
    for txn in transactions:
        running = round(running + txn["amount"], 2)
        txn["balance"] = running

    totals = []
    for label, kind in (("Total incoming:", "positive"), ("Total outgoing:", "negative")):
        value = next(money(t) for line in lines for text, _, x2 in line["items"] if label in text
                     for t, a, _ in line["items"] if a > x2 and money(t) is not None)
        totals.append({"label": label.rstrip(":"), "kind": kind, "value": abs(value)})
    return {"bank_name": bank, "account_number": account,
            "opening_balance": opening, "closing_balance": closing,
            "transactions": transactions, "totals": totals}


if __name__ == "__main__":
    statement = parse(pl.load_frames()[0])
    print(statement)
    pl.reconcile([t["amount"] for t in statement["transactions"]],
                 statement["opening_balance"], statement["closing_balance"],
                 [t["balance"] for t in statement["transactions"]])