"""Canned converged probe script for Gravy Toast Bank (test1.pdf).

What a successful REPL run should converge to: probe anchors, locate the
header row, classify the table region, verify formats actually parse,
reconcile the signed amounts, then build_config with verified parameters.
No statement values are transcribed — everything below is derived from the
frames at runtime. Used to test the distill harness without the LLM.
"""

import distill_emulator as emu

frames = emu.load_frames()
f = frames[0]
lines = f["lines"]
emu.show(lines, 12)

# --- Label anchors (the y1 fields) ------------------------------------------

anchor = {
    "account number": emu.anchor_value(lines, "Account number:"),
    "opening balance": emu.anchor_value(lines, "Opening balance:"),
    "closing balance": emu.anchor_value(lines, "Closing balance:"),
    "statement period": emu.anchor_value(lines, "Statement Period:"),
}
for field, value in anchor.items():
    print(f"{field}: {value}")

opening_words = anchor["opening balance"]
closing_words = anchor["closing balance"]
opening_fmt = emu.amount_format_of(opening_words)
closing_fmt = emu.amount_format_of(closing_words)
period_fmt = emu.date_format_of(anchor["statement period"][0]) if anchor["statement period"] else []
print(f"opening format: {opening_fmt}, closing format: {closing_fmt}, period: {period_fmt}")
opening = emu.parse_amount(opening_words, opening_fmt)
closing = emu.parse_amount(closing_words, closing_fmt)

# --- Column header row and spans ---------------------------------------------

header = emu.header_line(lines, "Date", "Description", "Balance")
spans = emu.spans(header)
print("header spans:", spans)

# --- Table region -------------------------------------------------------------

first, last = emu.region(lines, "Transaction Details", ["Transactions stop here."])
header_index = lines.index(header)
rows, orders = emu.classify(
    lines,
    {
        "date": spans["Date"],
        "description": spans["Description"],
        "debit": spans["Debit"],
        "amount": spans["Credit"],
        "balance": spans["Balance"],
    },
    first=header_index + 1,
    last=last,
)
print(f"table rows: {len(rows)}")

# --- Verify formats parse; assemble signed amounts ---------------------------

credit_sample = next(row["amount"] for row in rows if row["amount"])
balance_sample = next(row["balance"] for row in rows if row["balance"])
amount_fmt = emu.amount_format_of(credit_sample)
balance_fmt = emu.amount_format_of(balance_sample)
print(
    f"amount sample {credit_sample!r} -> {amount_fmt}; "
    f"balance sample {balance_sample!r} -> {balance_fmt}"
)

amounts = emu.signed_amounts(rows)
bad_balance = 0
for row in rows:
    if row["balance"] and emu.parse_amount(row["balance"], balance_fmt) is None:
        bad_balance += 1
print(
    f"{bad_balance} balance token(s) match no engine format "
    "(e.g. ungrouped digits) — balance-less row orders must also be declared"
)

# --- Local balance check before building the config ---------------------------

emu.reconcile(amounts, opening, closing)

# --- Observed row shapes (debit/credit both model as "amount") ----------------
# Only shapes that bind an amount belong in transaction_formats: a line with
# no money token is a wrapped-description continuation the engine merges into
# the previous transaction by itself (amount-less orders create amount-less
# transactions and fail the checkers).

transaction_formats = emu.transaction_shapes(orders)
print("observed orders:", transaction_formats)

# --- The form ------------------------------------------------------------------

config = emu.build_config(
    key="us__gravytoastbank__statement__2",
    account_type="Checking",
    bank_name="Gravy Toast Bank",
    account_terms=["Gravy Toast Bank", "Fake Monthly Statement"],
    account_number_terms=["Account number:"],
    account_number_patterns=[emu.pattern_for(anchor["account number"][0])],
    account_number_alignment="y1",
    opening_balance_terms=["Opening balance:"],
    opening_balance_formats=[opening_fmt],
    opening_balance_alignment="y1",
    closing_balance_terms=["Closing balance:"],
    closing_balance_formats=[closing_fmt],
    closing_balance_alignment="y1",
    start_date_terms=["Statement Period:"],
    start_date_formats=["format2"],
    start_date_alignment="y1",
    transaction_terms=["Transaction Details"],
    transaction_terms_stop=["Transactions stop here."],
    transaction_formats=transaction_formats,
    transaction_date_formats=["format1"],
    transaction_date_headers=["Date"],
    transaction_date_alignment="x1",
    transaction_description_headers=["Description"],
    transaction_description_alignment="x1",
    transaction_amount_formats=[amount_fmt],
    transaction_amount_headers=["Credit"],
    transaction_amount_alignment="x2",
    transaction_amount_invert_headers=["Debit"],
    transaction_amount_invert_alignment="x2",
    transaction_balance_formats=[balance_fmt],
    transaction_balance_headers=["Balance"],
    transaction_balance_alignment="x2",
)
print("config distilled:", config["key"])
