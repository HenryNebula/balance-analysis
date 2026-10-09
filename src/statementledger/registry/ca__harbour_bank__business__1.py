import re
import statementledger as pl

KEY = "ca__harbour_bank__business__1"


def parse(frames):
    lines = frames["lines"]

    def text(line):
        return " ".join(item[0] for item in line["items"])

    def find_line(term):
        return next(line for line in lines if term.lower() in text(line).lower())

    def money(token):
        raw = token.replace("\u00a0", "").replace(" ", "").replace("$", "")
        if not re.fullmatch(r"[+-]?\d+(?:,\d{1,2})?", raw):
            return None
        return float(raw.replace(",", "."))

    def labeled_money(term):
        line = find_line(term)
        parts = [t for t, x1, _ in line["items"]
                 if x1 >= 280 and re.fullmatch(r"\d+(?:,\d{1,2})?", t)]
        return money("".join(parts))

    period = next(text(line) for line in lines if re.search(r"\b20\d{2}\b", text(line)))
    year = int(re.search(r"\b20\d{2}\b", period).group())
    account_line = find_line("Compte:")
    account = " ".join(t for t, x1, _ in account_line["items"] if x1 > 471)
    opening = labeled_money("D'ouverture")
    closing = labeled_money("Fermeture")
    bank_name = " ".join(item[0] for item in lines[0]["items"])

    header_index = next(i for i, line in enumerate(lines)
                        if "et débits" in text(line).lower())
    header = lines[header_index]
    debit_center = credit_center = balance_center = None
    for label, x1, x2 in header["items"]:
        low = label.lower()
        if "débits" in low:
            debit_center = (x1 + x2) / 2
        elif "crédits" in low:
            credit_center = (x1 + x2) / 2
        elif low == "solde":
            balance_center = (x1 + x2) / 2

    transactions, pending_desc, last_date = [], "", None
    for line in lines[header_index + 1:]:
        if "frais sur compte:" in text(line).lower():
            break
        cells, desc, date_parts = ["", "", ""], [], []
        for token, x1, x2 in line["items"]:
            cx = (x1 + x2) / 2
            if x1 < 95:
                date_parts.append(token)
            elif 95 <= x1 < 240:
                desc.append(token)
            elif x1 >= 270:
                centers = (debit_center, credit_center, balance_center)
                if all(center is not None for center in centers):
                    col = min(range(3), key=lambda n: abs(cx - centers[n]))
                    cells[col] = (cells[col] + " " + token).strip()

        date = pl.parse_date(" ".join(date_parts), default_year=year)
        debit, credit, balance = (money(cell) for cell in cells)
        if debit is None and credit is None:
            pending_desc = (pending_desc + " " + " ".join(desc)).strip()
            continue

        last_date = date or last_date
        transactions.append({
            "date": last_date,
            "description": (pending_desc + " " + " ".join(desc)).strip(),
            "amount": (credit or 0) - (debit or 0),
            "balance": balance,
        })
        pending_desc = ""

    running = opening
    for txn in transactions:
        running = round(running + txn["amount"], 2)
        if txn["balance"] is None:
            txn["balance"] = running

    return {
        "bank_name": bank_name,
        "account_number": account,
        "opening_balance": opening,
        "closing_balance": closing,
        "transactions": transactions,
    }


if __name__ == "__main__":
    statement = parse(pl.load_frames()[0])
    print(statement)
    pl.reconcile([t["amount"] for t in statement["transactions"]],
                 statement["opening_balance"], statement["closing_balance"],
                 [t["balance"] for t in statement["transactions"]])