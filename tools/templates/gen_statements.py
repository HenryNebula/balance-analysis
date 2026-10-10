"""Generate months of realistic statements by filling a real Word template.

Simulates what a bank's statement engine produces across a year: each
month's PDF varies in length (transaction count drives 1-4 table pages,
continuation pages repeat the column header row), long narratives flow
into date-less continuation rows, and a Terms & Conditions section closes
the document on its own page(s).

Templates are the TemplateLab Word originals (see fetch_templates.py):
© TemplateLab — the .docx templates, generated .docx/.pdf, truth JSONs
and baselines are all derivatives and stay in gitignored data/.

Pipeline per month: fill docx (python-docx, donor-row deepcopy keeps the
template's exact fonts/borders/widths) -> LibreOffice headless PDF (fixed
profile) -> pikepdf metadata normalization (byte-stable sha256s) ->
.truth.json with the exact ledger that was rendered, chained month to
month (closing = next opening).

    uv run python tools/templates/gen_statements.py --template tl-44769 \
        --year 2027 --months 12 --out data/gen
"""

from __future__ import annotations

import argparse
import calendar
import json
import random
import re
import subprocess
from copy import deepcopy
from pathlib import Path

import pikepdf
from docx import Document

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NS = {"w": W[1:-1]}

# --- fictional identity (no real bank, person, or account) ---------------------

BANK = "Meridian Trust Bank"
BANK_ADDR = ["4 Harborline Plaza, Suite 900", "Kingsport, NY 11204",
             "United States"]
BANK_SWIFT = "MERTRUS33"
BANK_EMAIL = "statements@meridiantrust.example"
BANK_SITE = "www.meridiantrust.example"
COMPANY = "Ackroyd & Vale Trading Co."
COMPANY_ADDR = ["Unit 7, 118 Ferrous Lane", "Copperfield Industrial Estate",
                "Kingsport, NY 11205", "United States"]
ACCOUNT_NO = "4305882700913"
CUSTOMER_ID = "88421077"
CURRENCY = "USD"
REF = "MER 0000-88421-003107"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Narratives a business current account actually sees. SHORT fits one
# PARTICULARS line (<=32 chars at 8pt Courier in the 3563-dxa column);
# LONG overflows and continues onto date-less rows, like the template's
# own "D/4301201810853001 ..." examples.
SHORT_DEBIT = [
    "CWDR/ATM 004273 KINGSPORT", "PRCR/CARD POS MARTELLO CAFE",
    "EFT/RENT 44 HARBORLINE PLZ", "BANK CHG/SMS ALERT FEE",
    "CWDR/BRANCH TELLER 0344", "PRCR/CARD POS VELLUM BOOKS",
    "UTILITY ACH/HARBOR POWER", "CHQ PAID/000451",
    "PRCR/CARD POS LARKSPUR FUEL", "CWDR/ATM 001885 JENGRE RD",
    "BANK CHG/ACCT MAINT FEE", "PRCR/CARD POS ANVIL HARDWARE",
    "NIP OUTWARD/QUARRYLANE CO", "TAX ACH/NYS-PTET",
]
SHORT_CREDIT = [
    "CASH DEP TELLER 0344", "EFT/INWARD T/T RK PLASTICS",
    "NIP INWARD/SEXTANT FOODS", "TRF FROM SAVINGS 9211",
    "CHEQUE DEP/000518", "EFT/INWARD SALARY A&V",
    "INT/CAPITAL CREDIT", "REFUND/POS MARTELLO",
    "NIP INWARD/OSPREY FREIGHT", "CASH DEP NIGHT SAFE 0341",
    "EFT/INWARD T/T BRIGHT MOOR", "TRF FROM OVERDRAFT 9214",
]
LONG_DEBIT = [
    "NIP OUTWARD TO/TANSEAC LOGISTICS REF:093820174651203",
    "D/4305882700913 PRCR/CARD POS QUARRYMEN BUILDERS YARD",
    "EFT/CBL REMIT TO/HELVETIA PARTS GENEVA INV-2291",
    "NIP OUTWARD TO/BRAMBLE LINE FARMING REF:550211778004",
    "D/4305882700913 CWDR/ATM 004273 KINGS PORT EAST 3RD ST",
]
LONG_CREDIT = [
    "NIP INWARD FROM/SEXTANT FOODS LTD REF:771204660023118",
    "EFT/INWARD T/T RK PLASTICS GMBH INV-2290 COVER",
    "D/4305882700913 CHEQUE DEP/000518 TELLER 0344 BRANCH",
    "NIP INWARD FROM/OSPREY FREIGHT REF:889031122045",
]
# 3-liners (66-95 chars): narration + instrument ref + settlement detail,
# the way business-account rows actually print
LONG3_DEBIT = [
    "NIP OUTWARD TO/OSPREY FREIGHT LTD REF:889031122045 SETTLED VIA "
    "INSTANT PAYMENT GATEWAY SAME DAY",
    "D/4305882700913 PRCR/CARD POS QUARRYMEN BUILDERS YARD KINGSPORT "
    "EAST AUTH CODE 0442",
    "EFT/CBL REMIT TO/HELVETIA PARTS GMBH GENEVA INV-2291 LESS BANK "
    "CHARGES 12.50",
]
LONG3_CREDIT = [
    "NIP INWARD FROM/SEXTANT FOODS LTD REF:771204660023118 COVER "
    "INVOICE 4471 SEXTANT FOODS",
    "D/4305882700913 CHEQUE DEP/000518 TELLER 0344 BRANCH KINGSPORT "
    "CLEARING 2 BUSINESS DAYS",
]
CHQ_ROWS = {"CHQ PAID/000451"}  # narratives that also carry a CHQ.NO

# --- tl-44773 (Chase-style checking): sectioned statement, 5 tables -----

# Real Chase statements keep the deposit section's DESCRIPTION column
# sparse and repetitive; a few variants stress that habit.
CHASE_DEPOSIT_DESCS = [
    "Deposit", "Deposit", "Deposit", "Deposit",      # the common case
    "Mobile Deposit", "Online Transfer From Sav 9211",
    "Check Deposit", "Interest Paid", "Wire Inward RK Plastics",
]
CHASE_WITHDRAWAL_DESCS = [
    "Online Payment XXXXX To Vendor 23311802",   # wraps (long)
    "Online Payment XXXXX To Vendor 77124563",   # wraps (long)
    "ADP TX/Fincl Svc ADP",
    "ATM Withdrawal 004273 Kingsport",
    "Debit Card Purchase MARTELLO CAFE",
    "Debit Card Purchase VELLUM BOOKS",
    "Monthly Service Fee",
    "Wire Outward To/Tanseac Logistics",
    "Auto Pay Harbor Power Electric",
]
# amount ranges follow the narrative type — a $6,000 "service fee" reads
# as fabricated; plausible ranges per description prefix
CHASE_WDR_RANGES = {
    "Monthly Service Fee": (12, 45),
    "ATM Withdrawal": (40, 400),
    "Debit Card Purchase": (6, 320),
    "Auto Pay": (60, 640),
    "ADP TX": (800, 4200),
    "Online Payment": (120, 9200),
    "Wire Outward": (600, 9500),
}
CHASE_CUSTOMER = "Nordvale Tool Co"
CHASE_ACCOUNT_LINE = "00056784 DDA 001 KY 33417 – YYN T 1 00000000 11 0000"
CHASE_ACCOUNT_NO = "005678421483"

TNC_HEADING = "TERMS AND CONDITIONS"
TNC_INTRO = ("These conditions apply to the account stated above and to all "
             "transactions listed in this statement of account.")
TNC_CLAUSES = [
    "The Bank records transactions on the date they are processed and may "
    "process an instruction on a later business day than the day it was "
    "given.",
    "Balances shown in this statement are computed at the close of each "
    "business day and include transactions processed on that day.",
    "The account holder must examine this statement carefully and report "
    "any apparent error or unauthorised transaction within thirty days "
    "of the issue date.",
    "Cheques and other instruments deposited are subject to collection; "
    "funds may be withdrawn only after proceeds are received by the Bank.",
    "The Bank may debit the account for charges, fees, and corrections "
    "relating to transactions previously credited or paid.",
    "Electronic transfers are executed under the account holder's "
    "instructions and the Bank is not liable for delays caused by other "
    "participating institutions.",
    "Interest, where payable, accrues on the daily closing balance and "
    "is credited on the last business day of the calendar quarter.",
    "The Bank may amend these conditions by written notice delivered "
    "with any future statement or through its published tariff guide.",
    "This statement is issued in duplicate; the account holder should "
    "retain statements for no less than seven years for tax purposes.",
    "If any provision of these conditions is unenforceable, the "
    "remaining provisions continue in full force.",
    "The governing law is that of the jurisdiction in which the "
    "maintaining branch is situated.",
    "Computer-generated statements carry no signature and are valid "
    "without seal or manual attestation.",
]
TNC_CLOSING = ["For " + BANK + " — Account Services",
               "This statement is computer generated and requires no signature."]


# --- ledger synthesis ----------------------------------------------------------

def split_lines(text: str, width: int = 32) -> list[str]:
    """Word-wrap a narrative into PARTICULARS lines that fit one row each."""
    words, lines, cur = text.split(), [], ""
    for word in words:
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines


def month_ledger(rng: random.Random, year: int, month: int, opening: float,
                 n_txns: int) -> tuple[list[dict], float]:
    """Synthesize one month: txns with printed rows, chained running balance.

    Presentation variation is deliberate (each breaks a naive parser
    assumption): narratives of 1-3 printed lines; CHQ.NO populated on
    cheque/branch rows and blank otherwise; the inactive money column
    blank rather than '0.00' on some rows; ~10% of rows omit the printed
    balance (truth still records the computed running balance)."""
    days_in_month = calendar.monthrange(year, month)[1]
    txns, running = [], opening
    day = rng.randint(1, 3)
    for _ in range(n_txns):
        day = min(days_in_month, day + rng.choice((0, 0, 1, 1, 1, 2, 3)))
        # a real customer tops up before the overdraft gets silly
        debit = rng.random() < 0.62 if running > 2000 else False
        amount = round(rng.uniform(18, 9400) if debit else rng.uniform(90, 11800), 2)
        signed = -amount if debit else amount
        running = round(running + signed, 2)
        r = rng.random()
        if r < 0.10:                       # 3-line narration
            narrative = rng.choice(LONG3_DEBIT if debit else LONG3_CREDIT)
        elif r < 0.30:                     # 2-line narration
            narrative = rng.choice(LONG_DEBIT if debit else LONG_CREDIT)
        else:
            narrative = rng.choice(SHORT_DEBIT if debit else SHORT_CREDIT)
        # cheque-linked and branch rows carry an instrument number
        chq = ""
        if "CHQ" in narrative or "BRANCH TELLER" in narrative:
            if rng.random() < 0.8:
                chq = f"{rng.randint(0, 999999):06d}"
        # inactive money column: usually a printed 0.00, sometimes blank
        blank_zero = rng.random() < 0.15
        txns.append({
            "date": f"{day:02d}-{MONTHS[month - 1]}-{year}",
            "lines": split_lines(narrative),
            "chq": chq,
            "withdraw": f"{amount:,.2f}" if debit else ("" if blank_zero else "0.00"),
            "deposit": ("" if blank_zero else "0.00") if debit else f"{amount:,.2f}",
            "print_balance": rng.random() > 0.10,
            "amount": signed,
            "balance": running,
        })
    return txns, running


def truth_dict(opening: float, closing: float, txns: list[dict]) -> dict:
    """Canonical statement dict: continuation lines join into one
    description; dates are ISO (the statement prints dd-Mon-yyyy, the
    canonical ledger speaks YYYY-MM-DD)."""
    def iso(printed: str) -> str:
        day, mon, year = printed.split("-")
        return f"{year}-{MONTHS.index(mon) + 1:02d}-{int(day):02d}"

    return {
        "bank_name": BANK, "account_number": ACCOUNT_NO,
        "opening_balance": opening, "closing_balance": closing,
        "transactions": [
            {"date": iso(t["date"]), "description": " ".join(t["lines"]),
             "amount": t["amount"], "balance": t["balance"]} for t in txns
        ],
    }


# --- tl-44773 synthesis: one ledger, five agreeing tables -----------------------

CHASE_BANK = "JPMorgan Chase Bank N.A."


def synth_44773(rng: random.Random, year: int, month: int, opening: float,
                n_dep: int, n_chk: int, n_wdr: int) -> dict:
    """Build the Chase-style month: one ledger partitioned into deposits /
    checks / withdrawals, a daily-balance series, and every cross-total the
    template prints (summary rows, per-section totals, fee counters)."""
    import datetime as dt
    days_in = calendar.monthrange(year, month)[1]
    deposits, checks, withdrawals = [], [], []
    running = opening
    chk_no = rng.randint(1200, 2400)
    # transactions cluster onto a handful of activity days (payroll,
    # batch-settlement days) the way real business accounts move — this
    # also bounds the DAILY ENDING BALANCE table, which shares page 3
    activity_days = sorted(rng.sample(range(2, days_in), rng.randint(7, 10)))

    def add(bucket, desc, amount, day):
        nonlocal running
        running = round(running + amount, 2)
        bucket.append({"day": day, "desc": desc, "amount": amount,
                       "balance": running})

    def a_day():
        return rng.choice(activity_days)

    for _ in range(n_dep):
        # deposits throttle when the balance runs high, so months trend
        # like a real account instead of monotonically up
        hi = 18000 if running < 120000 else 5200
        add(deposits, rng.choice(CHASE_DEPOSIT_DESCS),
            round(rng.uniform(180, hi), 2), a_day())
    for _ in range(n_chk):
        add(checks, f"Check {chk_no}", -round(rng.uniform(120, 4200), 2),
            a_day())
        chk_no += rng.choice((1, 1, 2))
    for _ in range(n_wdr):
        desc = rng.choice(CHASE_WITHDRAWAL_DESCS)
        lo, hi = next((r for p, r in CHASE_WDR_RANGES.items()
                       if desc.startswith(p)), (50, 4000))
        add(withdrawals, desc, -round(rng.uniform(lo, hi), 2), a_day())

    deposits.sort(key=lambda t: t["day"])
    checks.sort(key=lambda t: t["day"])
    withdrawals.sort(key=lambda t: t["day"])

    # balances must be CHRONOLOGICAL end-of-day values: recompute the
    # running balance over all sections merged by day (the buckets
    # accumulated in draw order, which is not calendar order — the
    # DAILY ENDING BALANCE table and the truth ledger both need this)
    chronological = sorted(deposits + checks + withdrawals,
                           key=lambda t: t["day"])
    running = opening
    for t in chronological:
        running = round(running + t["amount"], 2)
        t["balance"] = running

    # daily ending balance for each day with activity (printed two pairs
    # per row, row-major, in the DAILY ENDING BALANCE table)
    by_day = {t["day"]: t["balance"] for t in chronological}
    daily = sorted(by_day.items())

    end = dt.date(year, month, days_in)
    return {
        "year": year, "month": month,
        "period_from": end.replace(day=1).strftime("%B %-d, %Y"),
        "period_to": end.strftime("%B %-d, %Y"),
        "opening": opening, "closing": running,
        "deposits": deposits, "checks": checks,
        "withdrawals": withdrawals, "daily": daily,
        "dep_total": round(sum(t["amount"] for t in deposits), 2),
        "chk_total": round(sum(-t["amount"] for t in checks), 2),
        "wdr_total": round(sum(-t["amount"] for t in withdrawals), 2),
    }


def truth_44773(m: dict) -> dict:
    """Canonical dict for the Chase-style statement: all sections merged
    chronologically with running balances recomputed in that order (they
    were accumulated per-bucket at synthesis; the daily table prints
    end-of-day values, which the recomputation reproduces)."""
    def iso(day: int) -> str:
        return f"{m['year']}-{m['month']:02d}-{day:02d}"

    txns = sorted(
        [{"date": iso(t["day"]), "description": t["desc"],
          "amount": t["amount"]} for t in
         m["deposits"] + m["checks"] + m["withdrawals"]],
        key=lambda t: t["date"])
    running = m["opening"]
    for t in txns:
        running = round(running + t["amount"], 2)
        t["balance"] = running
    assert abs(running - m["closing"]) < 0.005
    return {"bank_name": CHASE_BANK, "account_number": CHASE_ACCOUNT_NO,
            "opening_balance": m["opening"], "closing_balance": m["closing"],
            "transactions": txns}


def money(amount: float, first: bool, prefix: str = "$") -> str:
    """Chase prints the currency symbol on a section's first amount only."""
    return f"{prefix}{amount:,.2f}" if first else f"{amount:,.2f}"


def fill_44773(template: Path, out: Path, ctx: dict) -> None:
    doc = Document(template)
    m = ctx["model"]

    # header textbox: statement period + primary account. The box is
    # 153.95pt wide / 23.05pt tall — knife-edge for 'July 1, 2008 …', so
    # full month names wrap and push the account line out of the box;
    # widen it (right edge kept) and grow it a line. Footer: strip the
    # template-site watermark.
    import re as _re
    V = "{urn:schemas-microsoft-com:vml}"
    for section in doc.sections:
        for shape in section.header._element.iter(f"{V}shape"):
            style = shape.get("style", "")
            if "width:153.95pt" in style or "width:153.950pt" in style:
                style = (style.replace("width:153.95pt", "width:200pt")
                                .replace("width:153.950pt", "width:200pt")
                                .replace("margin-left:375.139587pt",
                                         "margin-left:329.09pt")
                                .replace("height:23.05pt", "height:32pt"))
                shape.set("style", style)
        for t in section.header._element.iter(f"{W}t"):
            if t.text and "through" in t.text:
                t.text = f"{m['period_from']} through {m['period_to']}"
            elif t.text and _re.fullmatch(r"\d{10,}", t.text.strip()):
                t.text = CHASE_ACCOUNT_NO
        for t in section.footer._element.iter(f"{W}t"):
            # the watermark text is split across runs; blank the whole
            # footer (it holds nothing else)
            t.text = ""

    paras = doc.paragraphs

    def set_p(prefix: str, text: str) -> None:
        matches = [p for p in paras if p.text.strip().startswith(prefix)]
        assert matches, f"no paragraph starts with {prefix!r}"
        set_para_text(matches[0], text)

    set_p("00013422", CHASE_ACCOUNT_LINE)
    set_p("Company Name", CHASE_CUSTOMER)

    # CHECKING SUMMARY + its two tab-formatted tail rows (the summary
    # prints section sums as NEGATIVE even though the sections print them
    # positive — keep the template's signed-vs-unsigned disagreement)
    t0 = doc.tables[0]
    set_tr_text(t0.rows[2]._tr, ["Beginning Balance", "",
                                 money(m["opening"], True)])
    set_tr_text(t0.rows[3]._tr, ["Deposits and Additions",
                                 str(len(m["deposits"])),
                                 f"{m['dep_total']:,.2f}"])
    set_tr_text(t0.rows[4]._tr, ["Checks Paid", str(len(m["checks"])),
                                 f"- {m['chk_total']:,.2f}"])
    # tab-formatted summary rows: the amounts' positions come from TAB
    # characters (python-docx converts \t in run text to <w:tab/>);
    # dropping them glues the amounts onto the labels
    set_p("Other Withdrawals, Fees",
          f"Other Withdrawals, Fees & Charges\t \t{len(m['withdrawals'])}"
          f"\t- {m['wdr_total']:,.2f}")
    n_all = len(m["deposits"]) + len(m["checks"]) + len(m["withdrawals"])
    set_p("Ending Balance",
          f"Ending Balance\t{n_all}\t{money(m['closing'], True)}")

    # DEPOSITS AND ADDITIONS ($ on the first amount row only)
    t1 = doc.tables[1]
    donor1 = deepcopy(t1.rows[1]._tr)
    for row in t1.rows[1:]:
        row._tr.getparent().remove(row._tr)
    anchor = t1.rows[0]._tr
    for i, t in enumerate(m["deposits"]):
        tr = deepcopy(donor1)
        set_tr_text(tr, [f"{m['month']:02d}/{t['day']:02d}", t["desc"],
                         money(t["amount"], i == 0)])
        anchor.addnext(tr)
        anchor = tr
    set_p("Total Deposits and Additions",
          f"Total Deposits and Additions\t{money(m['dep_total'], True)}")

    # CHECKS PAID — real check numbers with the template's footnote marker
    t2 = doc.tables[2]
    donor2 = deepcopy(t2.rows[1]._tr)
    total2 = t2.rows[-1]._tr
    for row in t2.rows[1:-1]:
        row._tr.getparent().remove(row._tr)
    for i, t in enumerate(m["checks"]):
        tr = deepcopy(donor2)
        no = t["desc"].split()[-1]
        set_tr_text(tr, [f"{no}\t^", "", f"{m['month']:02d}/{t['day']:02d}",
                         money(-t["amount"], i == 0)])
        total2.addprevious(tr)
    set_tr_text(total2, ["Total Checks Paid", "", "",
                         money(m["chk_total"], True)])

    # OTHER WITHDRAWALS, FEES & CHARGES — long descriptions wrap to a
    # second line; exact row heights must grow with the line count
    t3 = doc.tables[3]
    donor3 = deepcopy(t3.rows[1]._tr)
    for row in t3.rows[1:]:
        row._tr.getparent().remove(row._tr)
    anchor = t3.rows[0]._tr
    for i, t in enumerate(m["withdrawals"]):
        tr = deepcopy(donor3)
        n_lines = 2 if len(t["desc"]) > 30 else 1
        h = tr.find(f"{W}trPr/{W}trHeight")
        h.set(f"{W}val", str(229 + 130 * (n_lines - 1)))
        set_tr_text(tr, [f"{m['month']:02d}/{t['day']:02d}", t["desc"],
                         money(-t["amount"], i == 0)])
        anchor.addnext(tr)
        anchor = tr
    set_p("Total Other Withdrawals",
          f"Total Other Withdrawals, Fees & Charges"
          f"\t{money(m['wdr_total'], True)}")

    # DAILY ENDING BALANCE — DATE/AMOUNT pairs, row-major, $ on the very
    # first cell only; the last row's second pair may be blank
    t4 = doc.tables[4]
    donor4 = deepcopy(t4.rows[1]._tr)
    for row in t4.rows[1:]:
        row._tr.getparent().remove(row._tr)
    anchor = t4.rows[0]._tr
    for r in range((len(m["daily"]) + 1) // 2):
        tr = deepcopy(donor4)
        cells = ["", "", "", ""]
        for c in range(2):
            k = r * 2 + c
            if k < len(m["daily"]):
                day, bal = m["daily"][k]
                cells[2 * c] = f"{m['month']:02d}/{day:02d}"
                cells[2 * c + 1] = money(bal, k == 0)
        set_tr_text(tr, cells)
        anchor.addnext(tr)
        anchor = tr

    # service-charge counters (Chase counts items, not statement rows)
    set_p("Checks Paid / Debits",
          f"Checks Paid / Debits\t"
          f"{len(m['checks']) + len(m['withdrawals'])}")
    set_p("Deposits / Credits", f"Deposits / Credits\t{len(m['deposits'])}")
    set_p("Deposited Items", f"Deposited Items\t{len(m['deposits'])}")
    set_p("Transaction Total",
          f"Transaction Total\t"
          f"{len(m['checks']) + len(m['withdrawals']) + len(m['deposits'])}")

    # every table row is atomic — a printed row never splits across pages
    for tbl in doc.tables:
        for row in tbl.rows:
            trpr = row._tr.find(f"{W}trPr")
            if trpr is not None and trpr.find(f"{W}cantSplit") is None:
                trpr.append(trpr.makeelement(f"{W}cantSplit", {}))

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)


# --- tl-44745 (Chase Toronto billing statement): invoice ledger --------

# The invoice table is the ledger: Previous Service Charge (beginning
# due), Invoiced Payment Received, Current Service Charge, Total Due.
CHASE_T_CUSTOMER = "Nordvale"
CHASE_T_ADDR1 = "88 Ridge Road"
CHASE_T_ADDR2 = "Kingsport, ON M5V 2T6, Canada"
CHASE_T_ACCOUNT = "0000 01 000000077777777"

# Activity-detail grid: (volume, unit-price, charge, subtotal-group)
# paragraph indices inside the service textbox — fixed by the template.
# Groups: 0 ACCOUNT SERVICES, 1 E-PAYMENTS URGENT, 2 E-RECEIPTS URGENT,
# 3 CHEQUE/DRAFT/CASH, 4 LIQUIDITY SWEEPING.
CHASE_T_GRID = [
    # (vol_i, price_i, charge_i, group) — DOM paragraph indices
    (15, 17, 17, 0), (21, 23, 23, 0),
    (33, 35, 36, 1), (39, 41, 42, 1), (45, 47, 48, 1),
    (59, 61, 62, 2),
    (74, 76, 77, 3), (81, 83, 84, 3), (88, 90, 91, 3),
    (95, 97, 98, 3), (102, 104, 105, 3), (109, 111, 112, 3),
    (124, 126, 127, 4),
]
# group -> subtotal paragraph (G4's label says CHEQUE, DRAFT & CASH —
# a template labeling slip worth keeping)
CHASE_T_SUBTOTALS = [29, 54, 68, 119, 133]
CHASE_T_TOTAL = 139
# the template's own unit prices, in CHASE_T_GRID row order — kept fixed:
# the grid's cells are absolutely positioned with per-row width budgets
# (Chase prints sub-1 prices as '.8231'), and a wider price wraps the
# cell, growing the box and pushing rows onto the fixed-position rules
CHASE_T_PRICES = (8.0, 0.0, 20.0, 17.0, 22.0, 2.0,
                  30.0, 1.5, 0.0, 0.8, 9.0, 1.85, 15.0)
# charge cells whose paragraph holds the amount in its LAST text node
# (rows where volume/price and charge share a paragraph)
CHASE_T_CHARGE_LAST = {17: True, 23: True, 91: True}


def synth_44745(rng: random.Random, year: int, month: int,
                prev_due: float) -> dict:
    """One billing month: service lines (fixed categories, varying
    volumes/tariffs) -> subtotals -> total activity charge = the
    invoice's Current Service Charge; previous due chained from last
    month's total (statement cuts before the payment lands)."""
    import datetime as dt
    days_in = calendar.monthrange(year, month)[1]
    end = dt.date(year, month, days_in)
    issue = end + dt.timedelta(days=rng.randint(4, 8))   # 5th bus-day-ish
    due = end + dt.timedelta(days=30)
    pay_date = end + dt.timedelta(days=rng.randint(1, 10))
    prev_end = end.replace(day=1) - dt.timedelta(days=1)

    # base tariffs from the template, lightly varied per month
    rows = []
    for (vol_i, price_i, charge_i, group), price in zip(CHASE_T_GRID,
                                                        CHASE_T_PRICES):
        vol = rng.randint(1, 18)
        rows.append({"vol_i": vol_i, "price_i": price_i,
                     "charge_i": charge_i, "group": group,
                     "price": price, "vol": vol,
                     "charge": round(price * vol, 2)})
    # keep every group subtotal under 1000: the subtotal cells print in
    # the template's 6-char width class (no separators, no 4th digit)
    for g in range(5):
        while sum(r["charge"] for r in rows if r["group"] == g) > 950:
            for r in rows:
                if r["group"] == g and r["vol"] > 1:
                    r["vol"] -= 1
                    r["charge"] = round(r["price"] * r["vol"], 2)
                    break
    subtotals = [round(sum(r["charge"] for r in rows if r["group"] == g), 2)
                 for g in range(5)]
    total = round(sum(subtotals), 2)
    return {
        "year": year, "month": month,
        "issue": issue, "due": due, "pay_date": pay_date,
        "period_from": end.replace(day=1), "period_end": end,
        "prev_end": prev_end, "prev_due": prev_due, "charge": total,
        "total_due": total,      # previous invoice paid in full
        "rows": rows, "subtotals": subtotals,
        "invoice_no": f"0671{rng.randint(10**12, 10**13 - 1):d}",
        "prev_invoice_no": f"0671{rng.randint(10**12, 10**13 - 1):d}",
        "statement_ref": f"0999-00-0000000{rng.randint(10**8, 10**9 - 1):d}"
                         f"-{year}",
    }


def truth_44745(m: dict) -> dict:
    """Canonical billing ledger: opening = previous due, transactions =
    payment received + current charge, closing = total due."""
    def iso(d) -> str:
        return d.strftime("%Y-%m-%d")

    return {
        "bank_name": "JPMORGAN CHASE BANK, N.A.",
        "account_number": CHASE_T_ACCOUNT,
        "opening_balance": m["prev_due"],
        "closing_balance": m["total_due"],
        "transactions": [
            {"date": iso(m["pay_date"]),
             "description": "Invoiced Payment Received",
             "amount": -m["prev_due"], "balance": 0.0},
            {"date": iso(m["period_end"]),
             "description": "Current Service Charge",
             "amount": m["charge"], "balance": m["total_due"]},
        ],
    }


def fill_44745(template: Path, out: Path, ctx: dict) -> None:
    doc = Document(template)
    m = ctx["model"]

    def d_short(d) -> str:
        return d.strftime("%d %b %Y")

    # global string swaps across every w:t (body tables, textboxes,
    # header/footer): dates, ids, identity
    swaps = {
        "07 Jan 2015": d_short(m["issue"]),
        "31 Dec 2014": d_short(m["period_end"]),
        "01 Dec 2014": d_short(m["period_from"]),
        "22 Dec 2014": d_short(m["pay_date"]),
        "31 Nov 2014": d_short(m["prev_end"]),   # template's invalid date
        "31 Jan 2014": d_short(m["due"]),        # template's year typo
        "06710000000000122": m["prev_invoice_no"],
        "06710000000000123": m["invoice_no"],
        # longest key first: the cover box prints the ref WITH an mmdd
        # date suffix the shorter key would leave behind
        "0999-00-000000011111111-20150107":
            m["statement_ref"] + m["issue"].strftime("%m%d"),
        "0999-00-000000011111111-2015": m["statement_ref"],
        "20 Jan 2015": d_short(m["due"]),       # Debit Value Date
        "ABC Company": CHASE_T_CUSTOMER,
        "ABC COMPANY": CHASE_T_CUSTOMER.upper(),
        "125 ANY STREET": CHASE_T_ADDR1,
        "CITY, STATE, POSTAL CODE or ZIP, COUNTRY": CHASE_T_ADDR2,
        "CITY, STATE, POSTAL CODE or ZIP": CHASE_T_ADDR2,
        "0000 01 000000077777777": CHASE_T_ACCOUNT,
        "0000000000011111111": "0000000000099448",   # Group No.
    }
    parts = [doc.element.body] + [s._element for s in doc.sections
                                  for s in (s.header, s.footer)]
    for part in parts:
        for t in part.iter(f"{W}t"):
            if not t.text:
                continue
            for old, new in swaps.items():
                if old in t.text:
                    t.text = t.text.replace(old, new)

    # split-node dates ('31 Dec' + ' ' + '2014') can't be caught by
    # per-node substring swaps: collapse any paragraph still carrying a
    # stale template year (the memo's "beginning July 1, 2015" boiler-
    # plate stays — it is the template's own notice text)
    for p in doc.element.body.iter(f"{W}p"):
        ts = p.findall(f".//{W}t")
        joined = "".join(t.text or "" for t in ts)
        if ("2014" in joined or "2015" in joined) \
                and not joined.startswith("As a reminder"):
            fixed = joined
            for old, new in swaps.items():
                if len(old) == 11:            # dd MMM yyyy keys only
                    fixed = fixed.replace(old, new)
            if fixed != joined:
                ts[0].text = fixed
                for t in ts[1:]:
                    t.text = ""

    # invoice-table ledger rows — values given per PHYSICAL cell (the
    # logical 7-column grid carries gridSpans that shift by row)
    t0 = doc.tables[0]

    def set_row(ri, cells):
        tr = t0.rows[ri]._tr
        assert len(tr.findall(f"{W}tc")) == len(cells), ri
        set_tr_text(tr, cells)

    set_row(13, [m["prev_invoice_no"], "Previous Service Charge",
                 d_short(m["prev_end"]), f"{m['prev_due']:,.2f}", "", ""])
    set_row(14, ["", "Invoiced Payment Received", d_short(m["pay_date"]),
                 "", "", f"-{m['prev_due']:,.2f}", ".00"])
    set_row(15, [m["invoice_no"], "Current Service Charge",
                 d_short(m["period_end"]), f"{m['charge']:,.2f}", "",
                 f"{m['total_due']:,.2f}"])
    set_row(16, ["", "", "Total Due CAD", "", "",
                 f"{m['total_due']:,.2f}"])
    # remittance stub
    set_row(18, ["", "", "Customer Name:", "", CHASE_T_CUSTOMER, ""])
    set_row(19, ["", "", "Account Number:", "", CHASE_T_ACCOUNT])
    set_row(20, ["", "", "Invoice Number:", "", m["invoice_no"], ""])
    set_row(21, ["Total Amount Due (CAD)", f"{m['total_due']:,.2f}",
                 "Invoice Date:", "", d_short(m["period_end"]), ""])
    set_row(22, ["", "", "Payment Due On:", "", d_short(m["due"]), ""])

    # activity-detail grid numbers inside the service textbox (the box
    # whose first paragraph text is 'Service Code')
    V = "{urn:schemas-microsoft-com:vml}"
    box = None
    for content in doc.element.body.iter(f"{W}txbxContent"):
        texts = [t.text for t in content.iter(f"{W}t")]
        if texts and texts[0] == "Service Code":
            box = content
            break
    assert box is not None, "service textbox not found"
    paras = box.findall(f".//{W}p")      # 141 cells nested one level down

    def set_cell(i, value, last=False):
        ts = paras[i].findall(f".//{W}t")
        (ts[-1] if last else ts[0]).text = value

    # grid cells: no thousands separators — the template never printed
    # one and the cells can't take the extra character
    for r in m["rows"]:
        set_cell(r["vol_i"], str(r["vol"]))
        price = f"{r['price']:.4f}"
        if r["price"] < 1:      # Chase drops the leading zero: '.8000'
            price = price.replace("0.", ".", 1)
        set_cell(r["price_i"], price)
        charge = f"{r['charge']:.2f}"
        if r["charge"] < 1:
            charge = charge.replace("0.", ".", 1)
        set_cell(r["charge_i"], charge,
                 last=bool(CHASE_T_CHARGE_LAST.get(r["charge_i"])))
    for group, para_i in enumerate(CHASE_T_SUBTOTALS):
        if para_i is not None:
            set_cell(para_i, f"{m['subtotals'][group]:.2f}", last=True)
    set_cell(CHASE_T_TOTAL, f"{m['total_due']:.2f}", last=True)

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)


def billing_fits(pdf_path: Path) -> bool:
    """The Toronto billing statement is a fixed 6-page package."""
    import pikepdf
    return len(pikepdf.open(pdf_path).pages) == 6


def chase_fits(pdf_path: Path) -> bool:
    """The Chase statement is exactly 4 pages ('Page N of 4' literals are
    template text): each section must stay on its page."""
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        if len(pdf.pages) != 4:
            return False
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if f"Page {i} of 4" not in text:
                return False
    return True


# --- docx text surgery (donor-copy keeps every template property) ---------------

def set_para_text(para, text: str) -> None:
    """Replace a paragraph's text, keeping the first text run's formatting.

    Template paragraphs smuggle layout in runs: this template positions
    its header with <w:br w:type="column"/> runs (column breaks), so any
    run carrying a break/drawing must survive a text swap — assigning
    .text to such a run would delete the break and rebalance the
    columns (seen: address lines jumping into the right-hand column).
    """
    structural = (f"{W}br", f"{W}drawing", f"{W}pict")
    runs = para.runs
    text_run, keep = None, []
    for r in runs:
        if any(r._r.find(tag) is not None for tag in structural):
            keep.append(r)                      # never touch layout runs
        elif text_run is None:
            text_run = r
    if text_run is None:
        text_run = para.add_run(text)
    else:
        text_run.text = text
    for r in runs:
        if r is not text_run and r not in keep:
            r._r.getparent().remove(r._r)


def set_tr_text(tr, texts: list[str]) -> None:
    """Set one line of text in each cell of a copied <w:tr>, keeping
    format. Some template cells carry their text in a LATER paragraph
    (first one is spacing-only): the paragraph holding the first text
    run becomes the keeper; other paragraphs are dropped. Like
    set_para_text, runs carrying breaks/drawings are never touched."""
    structural = (f"{W}br", f"{W}drawing", f"{W}pict")
    for tc, text in zip(tr.findall(f"{W}tc"), texts):
        target_p = text_r = None
        for p in tc.findall(f"{W}p"):
            for r in p.findall(f"{W}r"):
                if any(r.find(tag) is not None for tag in structural):
                    continue
                if text_r is None:
                    target_p, text_r = p, r
                    break
            if text_r is not None:
                break
        if text_r is None:            # no text run anywhere: first para
            target_p = tc.findall(f"{W}p")[0]
        for p in tc.findall(f"{W}p"):
            if p is not target_p:
                tc.remove(p)
        if text_r is not None:
            ts = text_r.findall(f"{W}t")
            if not ts:
                t = text_r.makeelement(f"{W}t", {})
                text_r.append(t)
                ts = [t]
            for t in ts[1:]:
                text_r.remove(t)
            ts[0].text = text
        for r in target_p.findall(f"{W}r"):
            if r is not text_r and not any(
                    r.find(tag) is not None for tag in structural):
                target_p.remove(r)


def add_next(anchor_el, new_el):
    anchor_el.addnext(new_el)
    return new_el


def make_para_like(donor_para, text: str, break_before: bool = False):
    """Deepcopy a donor paragraph, swap its text (optionally page-break)."""
    p = deepcopy(donor_para._p)
    from docx.text.paragraph import Paragraph
    para = Paragraph(p, donor_para._parent)
    if break_before:
        set_para_text(para, "")
        run = para.add_run()
        br = run._r.makeelement(f"{W}br", {f"{W}type": "page"})
        run._r.append(br)
        return p
    set_para_text(para, text)
    return p


def fix_footer_numpages(doc) -> None:
    """The template's page footer is a 42.65pt-wide VML textbox holding
    'Page {PAGE} of 2' — the '2' is a hardcoded literal (wrong for any
    other length) and the box is too narrow, so it wrapped even in the
    original template. Rebuild the textbox content as 'Page {PAGE} of
    {NUMPAGES}' and widen the box to fit it on one line."""
    from docx.oxml import parse_xml
    V = "{urn:schemas-microsoft-com:vml}"
    footer = doc.sections[0].footer
    for pict in footer.paragraphs[0]._p.iter(f"{W}pict"):
        shape = pict.find(f".//{V}shape")
        box = pict.find(f".//{W}txbxContent")
        if shape is None or box is None:
            continue
        page_para = next((p for p in box.findall(f"{W}p")
                          if any((t.text or "").startswith("Page")
                                 for t in p.findall(f".//{W}t"))), None)
        if page_para is None:
            continue
        for r in page_para.findall(f"{W}r"):
            page_para.remove(r)
        frags = [
            f'<w:r xmlns:w="{NS["w"]}"><w:t xml:space="preserve">Page </w:t></w:r>',
            f'<w:r xmlns:w="{NS["w"]}"><w:fldChar w:fldCharType="begin"/></w:r>',
            f'<w:r xmlns:w="{NS["w"]}"><w:instrText xml:space="preserve"> PAGE '
            f'</w:instrText></w:r>',
            f'<w:r xmlns:w="{NS["w"]}"><w:fldChar w:fldCharType="separate"/></w:r>',
            f'<w:r xmlns:w="{NS["w"]}"><w:t>1</w:t></w:r>',
            f'<w:r xmlns:w="{NS["w"]}"><w:fldChar w:fldCharType="end"/></w:r>',
            f'<w:r xmlns:w="{NS["w"]}"><w:t xml:space="preserve"> of </w:t></w:r>',
            f'<w:r xmlns:w="{NS["w"]}"><w:fldChar w:fldCharType="begin"/></w:r>',
            f'<w:r xmlns:w="{NS["w"]}"><w:instrText xml:space="preserve"> NUMPAGES '
            f'</w:instrText></w:r>',
            f'<w:r xmlns:w="{NS["w"]}"><w:fldChar w:fldCharType="separate"/></w:r>',
            f'<w:r xmlns:w="{NS["w"]}"><w:t>1</w:t></w:r>',
            f'<w:r xmlns:w="{NS["w"]}"><w:fldChar w:fldCharType="end"/></w:r>',
        ]
        for frag in frags:
            page_para.append(parse_xml(frag))
        # widen the box (42.65pt -> 90pt), keep it centered on the page
        style = shape.get("style", "")
        style = style.replace("width:42.65pt", "width:90pt")
        style = style.replace("margin-left:293.5pt", "margin-left:261pt")
        shape.set("style", style)
        return


# --- template surgery helpers ---------------------------------------------------


def reserve_footer_band(doc) -> None:
    """The template's footer rule is an absolutely-positioned line at
    y=696.55pt, but the bottom margin only stops content at 700pt — no
    footer band reserved, so rows landing at a page's bottom print
    through the rule. Widen the bottom margin until content clears it
    (the template's own table never reached that low, which is why the
    mis-tuning was invisible)."""
    for sect in doc.element.body.findall(f".//{W}sectPr"):
        mar = sect.find(f"{W}pgMar")
        if mar is not None and int(mar.get(f"{W}bottom", "0")) < 2000:
            mar.set(f"{W}bottom", "2000")   # 100pt: content ends 692 < 696.55


def relax_tail_page_break(doc) -> None:
    """The template parks the reward-points tail behind a section break
    with NO w:type (absent type defaults to nextPage) — meant for a
    1-page statement, it orphans a near-empty page after multi-page
    tables. Make that break continuous so the tail follows the table.
    (The first sectPr after the table is the table's own section end —
    continuous — and is skipped; the typeless nextPage one follows it.)"""
    body = doc.element.body
    seen_table = False
    for child in body:
        if child.tag == f"{W}tbl":
            seen_table = True
            continue
        if not seen_table or child.tag != f"{W}p":
            continue
        ppr = child.find(f"{W}pPr")
        sect = ppr.find(f"{W}sectPr") if ppr is not None else None
        if sect is None:
            continue
        typ = sect.find(f"{W}type")
        is_next = typ is None or typ.get(f"{W}val") == "nextPage"
        if is_next:
            if typ is None:
                typ = sect.makeelement(f"{W}type", {})
                sect.insert(0, typ)
            typ.set(f"{W}val", "continuous")
            return


def flatten_reward_tail(doc, ctx: dict) -> None:
    """The template renders the reward-points tail as a 2-column section
    with a column break (labels left, ': values' right) — a positioning
    hack whose layout changes with where it lands: labels/values split
    across a page break, values orphaned, or two labels per row. Flatten
    it to plain 'Label : value' lines in a 1-column section and make the
    whole tail atomic (keepNext), like a real statement engine's block."""
    body = doc.element.body
    paras = doc.paragraphs          # one build: identity-stable for index()
    # The template's tail labels are NOT one per paragraph (one paragraph
    # holds "Earned …<tab>… Redeemed/Expired"), so re-texting them in
    # place cannot work. Replace the whole block with four clean
    # 'Label : value' lines cloned from a label paragraph.
    reward_prefixes = ("Reward Points For Acc No", "Reward Points Earned",
                       "Reward  Points", "Reward Points Available")
    labels = [p for p in paras
              if p.text.strip().startswith(reward_prefixes)]
    values = [p for p in paras
              if p.text.strip().startswith(":") and p.text.strip() != ":"]
    if not labels:
        return
    first_line_el = None
    first_i = paras.index(labels[0])
    values = [v for v in values if paras.index(v) > first_i][:4]
    donor_para = labels[0]
    donor = deepcopy(donor_para._p)
    lines = [
        f"Reward Points For Acc No : {ACCOUNT_NO}",
        f"Reward Points Earned In Current Month : {ctx['reward_earned']:.3f}",
        f"Reward Points Redeemed/Expired : {ctx['reward_redeemed']}",
        f"Reward Points Available : {ctx['reward_available']}",
    ]
    anchor = donor_para._p                    # still in the tree
    for label in labels[1:]:
        label._p.getparent().remove(label._p)
    for value in values:
        value._p.getparent().remove(value._p)
    from docx.text.paragraph import Paragraph
    first_line_el = None
    for line in lines:
        el = deepcopy(donor)
        set_para_text(Paragraph(el, donor_para._parent), line)
        anchor.addprevious(el)
        if first_line_el is None:
            first_line_el = el
    anchor.getparent().remove(anchor)         # drop the original label
    # the tail's letterhead repeat reads as stray text under the totals;
    # blank it (the head-of-statement letterhead keeps the identity)
    repeats = [p for p in paras if p.text.strip() == COMPANY]
    if len(repeats) > 1:
        set_para_text(repeats[-1], "")
    # reward section: 2 cols -> 1 (only the one after the table)
    seen_table = False
    for child in body:
        if child.tag == f"{W}tbl":
            seen_table = True
        elif seen_table and child.tag == f"{W}p":
            ppr = child.find(f"{W}pPr")
            sect = ppr.find(f"{W}sectPr") if ppr is not None else None
            if sect is None:
                continue
            cols = sect.find(f"{W}cols")
            if cols is not None and cols.get(f"{W}num") == "2":
                cols.set(f"{W}num", "1")
                for col in list(cols):
                    cols.remove(col)
                break
    # atomic tail: keepNext from first reward line through END OF STATEMENT
    if first_line_el is not None:
        el = first_line_el
        end_marker = None
        while el is not None:
            texts = "".join(t.text or "" for t in el.findall(f".//{W}t"))
            if "END OF STATEMENT" in texts:
                end_marker = el
                break
            el = el.getnext()
        el = first_line_el
        while el is not None:
            ppr = el.find(f"{W}pPr")
            if ppr is None:
                ppr = el.makeelement(f"{W}pPr", {})
                el.insert(0, ppr)
            if ppr.find(f"{W}keepNext") is None:
                ppr.append(ppr.makeelement(f"{W}keepNext", {}))
            if el is end_marker:
                break
            el = el.getnext()


# --- template adapter: tl-44769 (DATE/PARTICULARS/CHQ.NO/W/D/B) ----------------

def fill_44769(template: Path, out: Path, ctx: dict) -> None:
    doc = Document(template)
    body_ps = doc.paragraphs
    fix_footer_numpages(doc)
    reserve_footer_band(doc)
    relax_tail_page_break(doc)

    def by_text(prefix: str) -> list:
        return [p for p in body_ps if p.text.strip().startswith(prefix)]

    # identity blocks
    for p in by_text("COMPANY NAME AND LOGO"):
        set_para_text(p, COMPANY)
    for p in by_text("CUSTOMER NAME"):
        set_para_text(p, "Attn: R. Ackroyd")   # contact, not a name repeat
    addr = (COMPANY_ADDR + [""] * 5)[:5]
    for i in range(1, 6):
        for p in by_text(f"ADDRESS {i}"):
            set_para_text(p, addr[i - 1])
    for p in by_text("Bank name"):
        set_para_text(p, BANK)
    baddr = (BANK_ADDR + [""] * 3)[:3]
    for i in range(1, 4):
        for p in by_text(f"Address {i}"):
            set_para_text(p, baddr[i - 1])
    for p in by_text("SWIFT"):
        set_para_text(p, f"SWIFT  : {BANK_SWIFT}")
    for p in by_text("E-mail"):
        set_para_text(p, f"E-mail : {BANK_EMAIL}")
    for p in by_text("Website"):
        set_para_text(p, f"Website : {BANK_SITE}")
    for p in by_text("Ref:"):
        set_para_text(p, f"Ref: {REF}")

    # metadata values: the five ": x" paragraphs after the label line
    labels_i = next(i for i, p in enumerate(body_ps)
                    if p.text.startswith("Customer Id Account No"))
    values = [f": {CUSTOMER_ID}", f": {ACCOUNT_NO}", ": CURRENT ACCOUNT",
              f": {CURRENCY}", f": {ctx['issue_date_long']}"]
    vi = 0
    for p in body_ps[labels_i + 1:]:
        if vi >= 5:
            break
        if p.text.strip().startswith(":"):
            set_para_text(p, values[vi])
            vi += 1
    for p in by_text("STATEMENT OF ACCOUNT"):
        set_para_text(p, f"STATEMENT OF ACCOUNT FOR THE PERIOD OF "
                         f"{ctx['period_from']} TO {ctx['period_to']}")

    # transaction table
    t = doc.tables[0]
    donor_txn = deepcopy(t.rows[2]._tr)
    donor_total = deepcopy(t.rows[14]._tr)
    header_tr = t.rows[0]._tr
    trpr = header_tr.find(f"{W}trPr")
    if trpr is None:
        trpr = header_tr.makeelement(f"{W}trPr", {})
        header_tr.insert(0, trpr)
    if trpr.find(f"{W}tblHeader") is None:
        trpr.append(trpr.makeelement(f"{W}tblHeader", {}))

    for row in t.rows[2:]:                       # keep 0 header, 1 forward
        row._tr.getparent().remove(row._tr)

    fwd = t.rows[1]._tr
    set_tr_text(fwd, ["", "Balance Forward", "", "0.00", "0.00",
                      f"{ctx['opening']:,.2f}"])
    total_tr = add_next(fwd, donor_total)        # anchor; txns insert before

    for txn in ctx["txns"]:
        tr = deepcopy(donor_txn)
        # the template pins row height to the narration's line count:
        # exact 268 for 1-line rows, 450 for its 2-line rows. Beyond that
        # the true per-line cost is the 12pt line pitch (240 twips) —
        # extrapolating the 450 rounding clipped 3rd lines by ~2pt
        heights = {1: "268", 2: "450"}
        n_lines = len(txn["lines"])
        h = tr.find(f"{W}trPr/{W}trHeight")
        h.set(f"{W}val", heights.get(n_lines, str(268 + 240 * (n_lines - 1))))
        # a printed transaction row never splits across pages; without
        # this, LO breaks a multi-line row at the page bottom (line 1
        # crossing the footer rule, continuation orphaned on the next)
        trpr = tr.find(f"{W}trPr")
        if trpr.find(f"{W}cantSplit") is None:
            trpr.append(trpr.makeelement(f"{W}cantSplit", {}))
        set_tr_text(tr, [txn["date"], txn["lines"][0], txn["chq"],
                         txn["withdraw"], txn["deposit"],
                         f"{txn['balance']:,.2f}" if txn["print_balance"]
                         else ""])
        # long narratives continue on extra paragraphs inside the
        # PARTICULARS cell (the template's own wrapped-row style), never
        # as separate rows: a short continuation row landing right after
        # a repeated page header collided with the header's border
        if len(txn["lines"]) > 1:
            tc = tr.findall(f"{W}tc")[1]
            base_p = tc.findall(f"{W}p")[0]
            for line in txn["lines"][1:]:
                extra = deepcopy(base_p)
                for r_el in extra.findall(f"{W}r")[1:]:
                    extra.remove(r_el)
                ts = extra.findall(f"{W}r/{W}t")
                if ts:
                    ts[0].text = line
                tc.append(extra)
        total_tr.addprevious(tr)
    set_tr_text(total_tr, ["", "", "",
                           f"{ctx['total_withdraw']:,.2f}",
                           f"{ctx['total_deposit']:,.2f}",
                           f"{ctx['closing']:,.2f}"])

    # reward tail: flattened to atomic 'Label : value' lines
    flatten_reward_tail(doc, ctx)

    # Terms & Conditions on their own page, after END OF STATEMENT
    end_p = by_text("**** END OF STATEMENT ****")[0]
    donor_plain = by_text("Ref:")[0]
    donor_head = by_text("STATEMENT OF ACCOUNT")[0]
    anchor = end_p._p
    anchor = add_next(anchor, make_para_like(donor_plain, "", break_before=True))
    anchor = add_next(anchor, make_para_like(donor_head, TNC_HEADING))
    anchor = add_next(anchor, make_para_like(donor_plain, TNC_INTRO))
    for i, clause in enumerate(ctx["tnc"], start=1):
        anchor = add_next(anchor,
                          make_para_like(donor_plain, f"{i}. {clause}"))
    for line in TNC_CLOSING:
        anchor = add_next(anchor, make_para_like(donor_plain, line))

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)


ADAPTERS = {"tl-44769": fill_44769, "tl-44773": fill_44773,
            "tl-44745": fill_44745}


# --- PDF conversion + normalization --------------------------------------------

def convert_pdf(docx_path: Path, outdir: Path, profile: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    profile_abs = profile.resolve().as_posix()
    subprocess.run(
        ["soffice", "--headless", "--norestore", "--nolockcheck",
         f"-env:UserInstallation=file://{profile_abs}",
         "--convert-to", "pdf", "--outdir", str(outdir), str(docx_path)],
        check=True, capture_output=True, timeout=180)
    return outdir / (docx_path.stem + ".pdf")


def normalize_pdf(path: Path) -> None:
    """Make the PDF byte-deterministic. LibreOffice varies two things per
    run: a /DocChecksum it drops into the trailer (of all places) and the
    file /ID — and pikepdf/qpdf insist on regenerating an /ID on every
    save. Pin the checksum with pikepdf, then binary-patch the trailer's
    /ID hex strings to a constant (same length, so offsets hold)."""
    import re as _re
    import pikepdf
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        fixed = "D:20260101000000+00'00'"
        pdf.docinfo["/CreationDate"] = fixed
        pdf.docinfo["/ModDate"] = fixed
        pdf.docinfo["/Producer"] = "statementledger template generator"
        pdf.docinfo["/Creator"] = "statementledger template generator"
        pdf.trailer["/DocChecksum"] = pikepdf.Name("/" + "0" * 32)
        pdf.save(path)
    data = bytearray(path.read_bytes())
    i = data.rfind(b"/ID [")
    assert i != -1, "no /ID in trailer"
    seg = bytes(data[i:i + 120])
    patched, n = _re.subn(rb"[0-9A-Fa-f]{32}", b"0" * 32, seg, count=2)
    assert n == 2, f"expected 2 ID halves, found {n}"
    data[i:i + 120] = patched
    path.write_bytes(data)


# --- driver ---------------------------------------------------------------------

def tail_fits(pdf_path: Path) -> bool:
    """True iff the reward tail and END OF STATEMENT land on the same
    page as the table's last row (no orphan tail page, no split)."""
    import re
    import pdfplumber
    last_date = reward = end = None
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            for w in page.extract_words():
                if re.fullmatch(r"\d{2}-[A-Z][a-z]{2}-\d{4}", w["text"]):
                    last_date = i
                elif w["text"] == "Redeemed/Expired":
                    reward = i
                elif w["text"] == "****":
                    end = i
    return last_date is not None and reward == last_date == end


def build_ctx_44769(args, month: int, opening: float, n: int, seed: str,
                    end) -> tuple[dict, list, float, dict]:
    from datetime import timedelta
    rng = random.Random(seed + "|ledger")
    txns, closing = month_ledger(rng, args.year, month, opening, n)
    assert abs(opening + sum(t["amount"] for t in txns) - closing) < 0.005
    mrng = random.Random(seed + "|misc")
    issue = end + timedelta(days=mrng.randint(2, 5))
    ctx = {
        "opening": opening, "closing": closing, "txns": txns,
        "period_from": f"01-{MONTHS[month - 1]}-{args.year}",
        "period_to": f"{end.day:02d}-{MONTHS[month - 1]}-{args.year}",
        "issue_date_long": issue.strftime("%B %d, %Y"),
        "total_withdraw": abs(sum(t["amount"] for t in txns
                                  if t["amount"] < 0)),
        "total_deposit": sum(t["amount"] for t in txns if t["amount"] > 0),
        "reward_earned": round(mrng.uniform(0, 220), 1),
        "reward_redeemed": mrng.choice((0, 0, 0, 1)) * mrng.randint(1, 90),
        "reward_available": mrng.randint(90, 420),
        # one bank, one T&C: real statements print the same terms (same
        # order) on every month's statement
        "tnc": TNC_CLAUSES,
    }
    return ctx, txns, closing, truth_dict(opening, closing, txns)


def build_ctx_44773(args, month: int, opening: float, n: int, seed: str,
                    end) -> tuple[dict, list, float, dict]:
    """n is the total transaction count; the section split is derived
    from it with its own stable stream."""
    rng = random.Random(seed + "|ledger")
    crng = random.Random(seed + "|sections")
    n_dep = max(6, round(n * crng.uniform(0.38, 0.5)))
    n_chk = max(2, round(n * crng.uniform(0.10, 0.2)))
    n_wdr = max(3, n - n_dep - n_chk)
    model = synth_44773(rng, args.year, month, opening, n_dep, n_chk, n_wdr)
    txns = model["deposits"] + model["checks"] + model["withdrawals"]
    return {"model": model}, txns, model["closing"], truth_44773(model)


def build_ctx_44745(args, month: int, opening: float, n: int, seed: str,
                    end) -> tuple[dict, list, float, dict]:
    """The billing ledger has a fixed shape: previous due (the driver's
    chained opening), one payment, one current charge — n is unused."""
    rng = random.Random(seed + "|billing")
    model = synth_44745(rng, args.year, month, opening)
    truth = truth_44745(model)
    return {"model": model}, truth["transactions"], \
        model["total_due"], truth


BUILDERS = {"tl-44769": build_ctx_44769, "tl-44773": build_ctx_44773,
            "tl-44745": build_ctx_44745}
FITS = {"tl-44769": tail_fits, "tl-44773": chase_fits,
        "tl-44745": billing_fits}

NOTES = {
    "tl-44769": ("presentation semantics: narration continues on date-less "
                 "lines inside PARTICULARS; the inactive money column is "
                 "sometimes blank; some rows print no balance — truth "
                 "records the computed running balance for those"),
    "tl-44773": ("presentation semantics: dates are MM/DD (year lives in "
                 "the header's period line); the currency symbol prints on "
                 "a section's first amount only; the summary prints debit "
                 "sections negative while the section totals print "
                 "positive; check numbers are in CHECK NUMBER with the "
                 "description empty; per-row balances are not printed "
                 "(daily table + summary endpoints only) — truth records "
                 "computed running balances; real bank name kept from the "
                 "template, fictional data, local-only corpus"),
    "tl-44745": ("billing-invoice semantics: opening = previous invoice's "
                 "total due (paid in full before this cut), transactions = "
                 "payment + current service charge, closing = total due; "
                 "the activity-detail service grid keeps the template's "
                 "fixed categories with varying volumes/tariffs whose "
                 "subtotals sum exactly to the invoice charge (the "
                 "template's own numbers disagreed by 3.63); template "
                 "typos (invalid '31 Nov', year-2014 due date) are "
                 "emitted correctly; the cover memo still says "
                 "'beginning July 1, 2015' — template boilerplate, kept "
                 "as the template's own notice; real bank name kept, "
                 "fictional data, local-only corpus"),
}


def variation_of(template: str, txns: list, ctx: dict) -> dict:
    if template == "tl-44769":
        return {
            "multiline_txns": sum(len(t["lines"]) > 1 for t in txns),
            "threeline_txns": sum(len(t["lines"]) > 2 for t in txns),
            "chq_filled": sum(bool(t["chq"]) for t in txns),
            "blank_money_cells": sum(
                (t["amount"] < 0 and t["deposit"] == "") or
                (t["amount"] > 0 and t["withdraw"] == "") for t in txns),
            "unprinted_balances": sum(not t["print_balance"] for t in txns),
        }
    m = ctx["model"]
    if template == "tl-44773":
        return {
            "checks": len(m["checks"]),
            "wrapped_withdrawals": sum(len(t["desc"]) > 30
                                       for t in m["withdrawals"]),
            "daily_balance_points": len(m["daily"]),
            "sparse_deposit_descs": sum(t["desc"] == "Deposit"
                                        for t in m["deposits"]),
        }
    return {
        "service_lines": len(m["rows"]),
        "subtotal_groups": len([s for s in m["subtotals"] if s]),
        "carried_prev_due": m["prev_due"] > 0,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--template", default="tl-44769", choices=sorted(ADAPTERS))
    ap.add_argument("--year", type=int, default=2027)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--min-txns", type=int, default=10)
    ap.add_argument("--max-txns", type=int, default=85)
    ap.add_argument("--seed", default="meridian")
    ap.add_argument("--out", default="data/gen")
    args = ap.parse_args()

    tpl_dir = Path(__file__).resolve().parents[2] / "data" / "tl_docx"
    template = tpl_dir / f"{args.template}.docx"
    out_root = Path(args.out) / args.template
    profile = Path(args.out) / ".lo_profile"

    manifest = []
    opening = None
    for month in range(1, args.months + 1):
        stem = f"{args.year}-{month:02d}"
        seed = f"{args.seed}|{args.template}|{args.year}|{month}"
        if opening is None:
            opening = round(random.Random(seed + "|opening")
                            .uniform(4000, 22000), 2)
        # independent streams: the ledger sequence is stable while the
        # txn count is nudged by the tail-fit feedback loop below
        n0 = random.Random(seed + "|n").randint(args.min_txns, args.max_txns)
        if args.template == "tl-44773":
            # 4 literal 'Page N of 4' pages: deposits share page 1 with
            # the summary, checks/withdrawals/daily share page 3 — the
            # sectioned layout caps total rows well below the ledger style
            n0 = min(n0, 20)
        from datetime import date, timedelta
        end = date(args.year, month,
                   calendar.monthrange(args.year, month)[1])

        n, pdf, txns, closing, truth = n0, None, [], opening, {}
        for attempt in range(9):                # fit-feedback loop
            ctx, txns, closing, truth = BUILDERS[args.template](
                args, month, opening, n, seed, end)
            docx_out = out_root / "docx" / f"{stem}.docx"
            ADAPTERS[args.template](template, docx_out, ctx)
            pdf = convert_pdf(docx_out, out_root / "pdf", profile)
            if FITS[args.template](pdf) or attempt == 8:
                break
            # 44769 grows past a bad page break; 44773 must SHRINK to
            # keep each section (and its literal 'Page N of 4') on one page
            n += 1 if args.template == "tl-44769" else -2
            n = max(n, 10)
        normalize_pdf(pdf)

        import hashlib
        pages = len(pikepdf.open(pdf).pages)
        (out_root / "truth" / f"{stem}.truth.json").parent.mkdir(
            parents=True, exist_ok=True)
        (out_root / "truth" / f"{stem}.truth.json").write_text(
            json.dumps(truth, indent=2))
        manifest.append({
            "file": str(pdf.relative_to(out_root)), "month": stem,
            "template": args.template, "transactions": len(txns),
            "pages": pages, "opening": opening, "closing": closing,
            "tail_fit_adjusted": n != n0,
            "notes": NOTES[args.template],
            "variation": variation_of(args.template, txns, ctx),
            "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        })
        print(f"{stem}: {len(txns):3d} txns  {pages} pp  "
              f"{opening:>12,.2f} -> {closing:>12,.2f}"
              f"{'  (n adjusted ' + str(n0) + '->' + str(n) + ')' if n != n0 else ''}")
        opening = closing

    (out_root / "manifest.jsonl").write_text(
        "\n".join(json.dumps(r) for r in manifest) + "\n")
    print(f"wrote {out_root / 'manifest.jsonl'}")


if __name__ == "__main__":
    main()
