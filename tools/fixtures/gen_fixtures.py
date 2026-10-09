"""Generate the synthetic statement fixtures: tests/fixtures/*.pdf + *.truth.json.

Every fixture is a fictional bank we made up (no real statement content,
per the repo's no-real-PDFs rule). The STRUCTURAL ideas — label-beside-value
headers, multi-page tables with repeated page furniture, wrapped description
rows, date-less and balance-less rows, a table-stop footer — are borrowed
from transtractor's synthetic test fixtures (MIT; see NOTICE), which is what
the engine was distilled against.

Because the generator knows the ledger it renders, ground truth is exact:
each .truth.json holds the canonical statement dict a correct parser must
produce (with the same presentation semantics the parsers implement: a row
without its own date inherits the previous row's; fixture A fills unprinted
balances with the running computed value, fixture B leaves them null).

Deterministic (seeded + reportlab invariant mode): rerunning reproduces
byte-identical fixtures.

    uv run python tools/fixtures/gen_fixtures.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from reportlab import rl_config
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

rl_config.invariant = 1  # byte-identical PDFs across runs

OUT = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
PAGE_H = letter[1]
FOOTER_A = "Synthetic fixture footer"   # furniture: identical on every page
FOOTER_B = "not part of the statement"


def y(top_down: float) -> float:
    """reportlab measures from the bottom; frames measure from the top."""
    return PAGE_H - top_down


def money(value: float) -> str:
    return f"{value:,.2f}"


def wrap_at_word(desc: str) -> tuple[str, str]:
    """Split a description on a word boundary so that head + " " + tail
    round-trips to exactly the original (the parsers join with one space)."""
    words = desc.split()
    cut = max(1, len(words) // 2)
    return " ".join(words[:cut]), " ".join(words[cut:])


# --- Fixture A: test1-style vocabulary (debit/credit columns, CR balances) ------

A_DESCRIPTIONS = [
    "Payroll deposit", "Rent payment", "Grocery run", "Utility bill electric",
    "Internet subscription", "Pharmacy pickup", "Restaurant lunch",
    "Bookshop order", "Train pass top-up", "Gym membership",
    "Phone bill", "Insurance premium", "Refund from retailer",
    "Transfer to savings", "Cash withdrawal", "Interest payment",
    "Coffee subscription", "Hardware store", "Dentist invoice",
    "Monthly donation", "Streaming service", "Bakery and cafe",
    "Fuel station", "Market vegetables",
]


def render_a(pdf_path: Path) -> dict:
    rng = random.Random(20260228)
    opening = 25000.0
    txns, running, day = [], opening, 1
    for i, desc in enumerate(A_DESCRIPTIONS):
        debit = rng.random() < 0.78
        amount = round(-(rng.uniform(8, 900) if debit else rng.uniform(10, 1500)), 2)
        running = round(running + amount, 2)
        txns.append({
            "date": f"2026-02-{min(day, 28):02d}", "description": desc,
            "amount": amount, "running": running,
            "wrap": rng.random() < 0.22,
            "print_date": i == 0 or rng.random() > 0.15,
            "print_balance": rng.random() > 0.12,
        })
        day += rng.choice((0, 1, 1, 1, 2))

    closing = txns[-1]["running"]
    c = canvas.Canvas(str(pdf_path), pagesize=letter)

    def page_frame() -> None:
        # title + table header + footer at the SAME y on every page, so the
        # parser's repeated-line (furniture) filter drops them
        c.drawString(72, y(49), "Copper Kettle Bank")
        c.drawString(78, y(188), "Date")
        c.drawString(150, y(188), "Description")
        c.drawRightString(329, y(188), "Debit")
        c.drawRightString(400, y(188), "Credit")
        c.drawRightString(503, y(188), "Balance")
        c.drawString(72, y(770), FOOTER_A)
        c.drawString(200, y(770), FOOTER_B)

    page_frame()
    c.drawString(77, y(64), "Synthetic Monthly Statement")
    c.drawString(77, y(89), "Statement Period:")
    c.drawString(269, y(89), "1 Feb 2026 to 28 Feb 2026")
    c.drawString(77, y(104), "Opening balance:")
    c.drawString(269, y(104), f"${money(opening)} CR")
    c.drawString(77, y(119), "Closing balance:")
    c.drawString(269, y(119), f"${money(closing)} CR")
    c.drawString(77, y(134), "Account number:")
    c.drawString(269, y(134), "1234 5678 9123 4567")
    c.drawString(77, y(166), "Transaction Details")

    truth, last_date, y_down = [], None, 206
    for txn in txns:
        if y_down > 560:                 # force pagination: exercise furniture
            c.showPage()
            page_frame()
            y_down = 206
        jitter = rng.choice((0, 0, 1))    # row lines only; binning tolerates 3
        baseline = y(y_down + jitter)
        desc = txn["description"]
        if txn["wrap"]:                   # description-only line first
            head, tail = wrap_at_word(desc)
            c.drawString(150, baseline, head)
            y_down += 17
            baseline = y(y_down)
            desc = tail
        if txn["print_date"]:
            c.drawString(78, baseline, txn["date"].replace("2026-02-", "") + " Feb")
            last_date = txn["date"]
        c.drawString(150, baseline, desc)
        c.drawRightString(329 if txn["amount"] < 0 else 400, baseline,
                          money(abs(txn["amount"])))
        if txn["print_balance"]:
            c.drawRightString(503, baseline, f"{money(txn['running'])} CR")
        truth.append({
            "date": last_date,           # date-less rows inherit (semantics)
            "description": txn["description"],
            "amount": txn["amount"],
            "balance": txn["running"] if txn["print_balance"] else None,
        })
        y_down += 17
    c.drawString(77, y(y_down + 6), "Transactions stop here.")
    c.save()

    # presentation semantics of parser A: unprinted balances become computed
    running_balance = opening
    for txn in truth:
        running_balance = round(running_balance + txn["amount"], 2)
        if txn["balance"] is None:
            txn["balance"] = running_balance
    return {"bank_name": "Copper Kettle Bank", "account_number": "1234 5678 9123 4567",
            "opening_balance": opening, "closing_balance": closing,
            "transactions": truth}


# --- Fixture B: PY_EXAMPLE vocabulary (single Amount column, signed money) ------

B_DESCRIPTIONS = [
    "Interest paid", "ATM withdrawal downtown", "Monthly transfer out",
    "Dividend deposit", "Utility autopay", "Wire from employer",
    "Card purchase bookstore", "Card purchase grocer",
    "Service fee refund", "Transfer from checking", "Tax refund",
    "Charity donation", "Hardware order screws", "Cash deposit branch",
]


def render_b(pdf_path: Path) -> dict:
    rng = random.Random(20260331)
    opening = 8214.60
    txns, running = [], opening
    for i, desc in enumerate(B_DESCRIPTIONS):
        amount = 42.17 if i == 0 else round(rng.uniform(-380, 520), 2)
        running = round(running + amount, 2)
        txns.append({
            "date": f"2026-03-{(i * 2) + 1:02d}", "description": desc,
            "amount": amount, "running": running,
            "wrap": i in (2, 7, 11), "print_balance": i not in (4, 9),
        })

    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    c.drawString(72, y(49), "Blue Lantern Savings Bank")
    c.drawString(77, y(89), "Account No:")
    c.drawString(200, y(89), "9876 5432")
    c.drawString(77, y(104), "Statement Period")
    c.drawString(230, y(104), "Mar 1, 2026 to Mar 31, 2026")
    c.drawString(77, y(119), "Beginning Balance")
    c.drawString(230, y(119), f"${money(opening)}")
    c.drawString(77, y(134), "Ending Balance")
    c.drawString(230, y(134), f"${money(txns[-1]['running'])}")
    c.drawString(78, y(188), "Date")
    c.drawString(150, y(188), "Description")
    c.drawRightString(380, y(188), "Amount")
    c.drawRightString(500, y(188), "Balance")

    truth, y_down = [], 206
    for txn in txns:
        jitter = rng.choice((0, 0, 1))
        baseline = y(y_down + jitter)
        desc = txn["description"]
        if txn["wrap"]:
            head, tail = wrap_at_word(desc)
            c.drawString(150, baseline, head)
            y_down += 17
            baseline = y(y_down)
            desc = tail
        c.drawString(78, baseline, txn["date"].replace("2026-03-", "") + "/03/2026")
        c.drawString(150, baseline, desc)
        amount_text = (f"({money(-txn['amount'])})" if txn["amount"] < 0
                       else money(txn["amount"]))
        c.drawRightString(380, baseline, amount_text)
        if txn["print_balance"]:
            c.drawRightString(500, baseline, money(txn["running"]))
        # B's parser: balance stays None where the statement prints none
        truth.append({"date": txn["date"], "description": txn["description"],
                      "amount": txn["amount"],
                      "balance": txn["running"] if txn["print_balance"] else None})
        y_down += 17
    c.drawString(77, y(y_down + 6), "End of activity")
    c.save()
    return {"bank_name": "Blue Lantern Savings Bank", "account_number": "9876 5432",
            "opening_balance": opening, "closing_balance": txns[-1]["running"],
            "transactions": truth}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fixtures = {
        "copperkettle_checking": render_a(OUT / "copperkettle_checking.pdf"),
        "bluelantern_savings": render_b(OUT / "bluelantern_savings.pdf"),
    }
    from statementledger.frames import frames_for

    import tempfile
    for name, truth in fixtures.items():
        # the ledger the generator rendered must itself reconcile
        running = truth["opening_balance"] + sum(t["amount"] for t in truth["transactions"])
        assert abs(running - truth["closing_balance"]) < 0.005, name
        (OUT / f"{name}.truth.json").write_text(json.dumps(truth, indent=2))
        # frames snapshot: the committed extraction of this PDF. A pdfplumber
        # upgrade that changes geometry shows up here first.
        with tempfile.TemporaryDirectory() as tmp:
            frames = frames_for([OUT / f"{name}.pdf"], Path(tmp))
        (OUT / f"{name}.frames.json").write_text(json.dumps(frames, indent=2))
        print(f"{name}.pdf: {len(truth['transactions'])} transactions, "
              f"{truth['opening_balance']:,.2f} -> {truth['closing_balance']:,.2f}")


if __name__ == "__main__":
    main()
