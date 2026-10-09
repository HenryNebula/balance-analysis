"""Canned lean-mode script for Gravy Toast (test1.pdf) — the pandas idiom
a successful --lean run should converge to: free-form wrangling on the
frames DataFrame, evidence-chosen formats, build_config last. Used to test
the lean harness deterministically without the LLM.
"""

import lean_emulator as le
import pandas as pd

df = le.frames()
print(df.head(12))

lines = df.groupby(["page", "line"], as_index=False).agg(
    text=("text", " ".join), y=("y", "first")
)
print(lines.tail(6))  # read the words that end the table


def beside(label: str) -> str:
    hit = df[df.text.str.contains(label, regex=False)]
    row = df[df.line == hit.line.iloc[0]]
    return " ".join(row[row.x1 > hit.x1.max()].text)


opening_text = beside("Opening balance:")
closing_text = beside("Closing balance:")
account = beside("Account number:")
print(opening_text, "|", closing_text, "|", account)

header_line = df[df.text == "Date"].line.iloc[0]
head = df[df.line == header_line].sort_values("cx")
print(head[["text", "x1", "x2"]])

stop_line = lines[lines.text.str.contains("Transactions stop here", regex=False)].line.iloc[0]
region = df[(df.line > header_line) & (df.line < stop_line)]
edges = [float("-inf")] + [(a + b) / 2 for a, b in zip(head.cx, head.cx.tolist()[1:])] + [float("inf")]
grid = region.assign(
    col=pd.cut(region.cx, bins=edges, labels=head.text.tolist())
).pivot_table(index="line", columns="col", values="text", aggfunc=" ".join)
print(grid.head(10))

# formats from evidence, signs from the Debit/Credit pair
amount_fmt = le.amount_format_of(grid["Credit"].dropna().iloc[0])
balance_fmt = le.amount_format_of(grid["Balance"].dropna().iloc[0])
date_fmt = le.date_format_of(grid["Date"].dropna().iloc[0])[0]
print("formats:", amount_fmt, balance_fmt, date_fmt)

amounts = []
for _, row in grid.iterrows():
    credit = row.get("Credit")
    debit = row.get("Debit")
    value = 0.0
    if isinstance(credit, str) and credit.strip():
        value += le.parse_amount(credit, None) or 0.0
    if isinstance(debit, str) and debit.strip():
        value -= le.parse_amount(debit, None) or 0.0
    amounts.append(value)
le.reconcile(amounts, opening_text, closing_text)

role = {"Date": "date", "Description": "description",
        "Credit": "amount", "Debit": "amount", "Balance": "balance"}
shapes: dict[tuple, int] = {}
for _, row in grid.iterrows():
    shape = []
    for column in head.text:
        if isinstance(row.get(column), str) and row[column].strip():
            mapped = role[column]
            if not shape or shape[-1] != mapped:
                shape.append(mapped)
    if "amount" in shape:
        shapes[tuple(shape)] = shapes.get(tuple(shape), 0) + 1
transaction_formats = [list(s) for s in sorted(shapes, key=shapes.get, reverse=True)]
print("orders:", transaction_formats)

le.build_config(
    key="us__gravytoastbank__statement__3",
    account_type="Checking",
    bank_name="Gravy Toast Bank",
    account_terms=["Gravy Toast Bank", "Fake Monthly Statement"],
    account_number_terms=["Account number:"],
    account_number_patterns=[le.pattern_for(account)],
    account_number_alignment="y1",
    opening_balance_terms=["Opening balance:"],
    opening_balance_formats=[le.amount_format_of(opening_text)],
    opening_balance_alignment="y1",
    closing_balance_terms=["Closing balance:"],
    closing_balance_formats=[le.amount_format_of(closing_text)],
    closing_balance_alignment="y1",
    start_date_terms=["Statement Period:"],
    start_date_formats=["format2"],
    start_date_alignment="y1",
    transaction_terms=["Transaction Details"],
    transaction_terms_stop=["Transactions stop here."],
    transaction_formats=transaction_formats,
    transaction_date_formats=[date_fmt],
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
print("config distilled")
