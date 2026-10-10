import re

import statementledger as pl

KEY = "us__fidelity__portfolio__1"


def parse(frames):
    lines = frames["lines"]

    def text(line):
        return " ".join(item[0] for item in line["items"])

    def labeled_amount(label):
        for line in lines:
            if re.match(r"\s*" + label + r"\b", text(line), re.I):
                for item_text, _x1, _x2 in line["items"][1:]:
                    amount = pl.parse_amount(item_text, strict_grouping=True)
                    if amount is not None:
                        return amount
        raise ValueError(f"label not printed: {label}")

    opening = labeled_amount("Beginning Portfolio Value")
    closing = labeled_amount("Ending Portfolio Value")
    labels = ("Additions", "Subtractions", "Change in Investment Value")
    transactions = []
    balance = opening
    for label in labels:
        for line in lines:
            row_text = text(line)
            if re.match(r"\s*" + label + r"\b", row_text, re.I):
                amount = next(
                    pl.parse_amount(item[0], strict_grouping=True)
                    for item in line["items"][1:]
                    if pl.parse_amount(item[0], strict_grouping=True) is not None
                )
                balance = round(balance + amount, 2)
                transactions.append({
                    "date": None,
                    "description": row_text,
                    "amount": amount,
                    "balance": balance,
                })
                break
        else:
            raise ValueError(f"label not printed: {label}")

    names = " ".join(text(line) for line in lines)
    accounts = list(dict.fromkeys(
        re.findall(r"\bAccount\s+([0-9][0-9-]+)", names, re.I)
    ))
    bank = re.search(r"\bFidelity\b", names, re.I)
    if not bank:
        raise ValueError("printed bank name not found")

    return {
        "bank_name": bank.group(),
        "account_number": ", ".join(accounts),
        "opening_balance": opening,
        "closing_balance": closing,
        "transactions": transactions,
    }


if __name__ == "__main__":
    statement = parse(pl.load_frames()[0])
    print(statement)
    pl.reconcile(
        [txn["amount"] for txn in statement["transactions"]],
        statement["opening_balance"],
        statement["closing_balance"],
        [txn["balance"] for txn in statement["transactions"]],
    )