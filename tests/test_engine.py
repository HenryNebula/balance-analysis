"""Engine invariants: the token parsers, the canonical schema, and the
arithmetic gate — the checks every registry parser lives or dies by."""

from pathlib import Path

import pytest

import statementledger as sl


@pytest.mark.parametrize("token,expected", [
    ("1,250.00", 1250.0),
    ("$2,100.55", 2100.55),
    ("500.00 DR", -500.0),
    ("500.00 CR", 500.0),
    ("(45.00)", -45.0),
    ("1,234.56-", -1234.56),
    ("-30.25", -30.25),
    ("", None),
    ("—", None),
    ("Nil", None),
    (["1,000.00", "CR"], 1000.0),
])
def test_parse_amount(token, expected):
    assert sl.parse_amount(token) == expected


def test_parse_amount_strict_grouping():
    assert sl.parse_amount("3,978.53", strict_grouping=True) == 3978.53
    assert sl.parse_amount("3978.53", strict_grouping=True) is None
    assert sl.parse_amount("3978.53") == 3978.53  # lenient reads it


@pytest.mark.parametrize("token,expected", [
    ("10 750,00 $", 10750.0),      # French: space thousands, comma decimal
    ("151,30 $", 151.30),
    ("+ 7 269,81 $", 7269.81),
    ("- 6 886,21 $", -6886.21),
    ("1.234,56", 1234.56),         # German: dot thousands
    ("1 234,56 CR", 1234.56),
    ("1,234", 1234.0),             # US 3-digit group stays thousands
    ("1,234.56", 1234.56),         # US cents untouched
])
def test_parse_amount_european(token, expected):
    assert sl.parse_amount(token) == expected


def test_parse_amount_european_strict_grouping():
    # grouping spans spaces in the Euro branch, mirroring commas in the US one
    assert sl.parse_amount("12 345,67", strict_grouping=True) == 12345.67
    assert sl.parse_amount("12345,67", strict_grouping=True) is None
    assert sl.parse_amount("12345,67") == 12345.67  # lenient reads it


@pytest.mark.parametrize("token,year,expected", [
    ("05 Mar", 2026, "2026-03-05"),
    ("5 March 2026", None, "2026-03-05"),
    ("Mar 5, 2026", None, "2026-03-05"),
    ("05/03/2026", None, "2026-03-05"),      # day-first convention
    ("25/12/2026", None, "2026-12-25"),      # unambiguous flips to month-first
    ("2026-03-05", None, "2026-03-05"),      # year leads: month-first pair
    ("05 Mar", None, None),                  # no year, no default
    ("Annoying text", None, None),
    ("123456", None, None),                  # bare digit run: an id
])
def test_parse_date(token, year, expected):
    assert sl.parse_date(token, default_year=year) == expected


def good_statement(**override):
    txn = {"date": "2026-02-01", "description": "Payroll", "amount": -100.0,
           "balance": 900.0}
    stmt = {"bank_name": "Bank", "account_number": "1", "opening_balance": 1000.0,
            "closing_balance": 900.0, "transactions": [txn]}
    stmt.update(override)
    return stmt


def test_validate_clean():
    assert sl.validate(good_statement()) == []


@pytest.mark.parametrize("mutation,fragment", [
    ({"opening_balance": "1,000.00"}, "opening_balance must be a number"),
    ({"bank_name": "  "}, "bank_name must be a non-empty string"),
    ({"transactions": []}, "transactions is empty"),
    ({"transactions": [{"description": "x", "amount": "oops"}]},
     "amount must be a number"),
    ({"transactions": [{"description": "x", "amount": 1.0, "date": "Feb 1"}]},
     "date must be an ISO"),
    ({"totals": [{"label": "T", "kind": "sideways", "value": 1}]}, "totals[0]"),
])
def test_validate_names_the_offense(mutation, fragment):
    errors = sl.validate(good_statement(**mutation))
    assert errors and fragment in "; ".join(errors)


def test_validate_rejects_non_dict():
    assert sl.validate(["not", "a", "dict"])


def test_reconcile_statement_clean():
    assert sl.reconcile_statement(good_statement()) is None


def test_reconcile_statement_closing_drift():
    failure = sl.reconcile_statement(good_statement(closing_balance=800.0))
    assert failure and "drift" in failure


def test_reconcile_statement_row_divergence():
    bad = good_statement()
    bad["transactions"][0]["balance"] = 500.0  # printed balance breaks the run
    failure = sl.reconcile_statement(bad)
    assert failure and failure.startswith("row 0")


def test_reconcile_statement_totals_check():
    stmt = good_statement(totals=[
        {"label": "Total withdrawals", "kind": "negative", "value": 200.0}])
    failure = sl.reconcile_statement(stmt)
    assert failure and "totals check" in failure


def test_reconcile_catches_unreadable_anchor():
    with pytest.raises(ValueError, match="opening"):
        sl.reconcile([1.0], "not money", 1.0)


# --- multi-account PDFs: parse() may return a LIST of ledgers ------------------


def test_ledgers_of():
    assert sl.ledgers_of(good_statement()) == [good_statement()]
    two = [good_statement(), good_statement()]
    assert sl.ledgers_of(two) is two


def test_validate_accepts_ledger_list():
    ledgers = [good_statement(account_number="817-1"),
               good_statement(account_number="817-2")]
    assert sl.validate(ledgers) == []


def test_validate_empty_ledger_list_is_an_error():
    errors = sl.validate([])
    assert errors and "empty list" in errors[0]


def test_validate_names_the_broken_ledger():
    ledgers = [good_statement(account_number="817-1"),
               good_statement(account_number="817-2", transactions=[])]
    errors = sl.validate(ledgers)
    assert errors and errors[0].startswith("ledger 1 (817-2):")
    assert "transactions is empty" in errors[0]


def test_reconcile_statement_gates_each_ledger():
    ledgers = [good_statement(account_number="817-1"),
               good_statement(account_number="817-2", closing_balance=800.0)]
    failure = sl.reconcile_statement(ledgers)
    assert failure and failure.startswith("ledger 1 (817-2):")


def test_evaluate_multi_ledger_verdict():
    statement = {"name": "x.pdf", "lines": []}

    def parse_fn(frames):
        return [good_statement(), good_statement()]

    verdict = sl.evaluate(parse_fn, [statement])
    assert verdict["verified"]
    assert verdict["results"][0]["rows"] == 2      # summed across ledgers
    assert verdict["results"][0]["ledgers"] == 2


def test_evaluate_multi_ledger_one_broken_fails_all():
    statement = {"name": "x.pdf", "lines": []}

    def parse_fn(frames):
        return [good_statement(), good_statement(closing_balance=800.0)]

    verdict = sl.evaluate(parse_fn, [statement])
    assert not verdict["verified"]
    assert "ledger 1" in verdict["results"][0]["error"]


def test_evaluate_coverage_pins_gate_partial_coverage():
    statement = {"name": "x.pdf", "lines": []}

    def one_ledger(frames):
        return good_statement()   # reconciles — but covers 1 of 2 accounts

    verdict = sl.evaluate(one_ledger, [statement], expect_ledgers={"x.pdf": 2})
    assert not verdict["verified"]
    assert "coverage" in verdict["results"][0]["error"]

    def two_ledgers(frames):
        return [good_statement(), good_statement()]

    assert sl.evaluate(two_ledgers, [statement],
                       expect_ledgers={"x.pdf": 2})["verified"]


def test_evaluate_coverage_pins_only_pinned_statements():
    # statements absent from the pin map carry no expectation
    statement = {"name": "x.pdf", "lines": []}

    def one_ledger(frames):
        return good_statement()

    assert sl.evaluate(one_ledger, [statement],
                       expect_ledgers={"other.pdf": 2})["verified"]


def test_evaluate_verdict_on_crash_with_hint():
    def crash(frames):
        return frames["lines"][0]["items"]["page"]  # list subscripted by str

    statement = {"name": "x.pdf",
                 "lines": [{"page": 0, "y": 1, "items": [["t", 1, 2]]}]}
    verdict = sl.evaluate(crash, [statement])
    assert not verdict["verified"]
    assert "HINT:" in verdict["results"][0]["error"]


def test_frames_extraction_is_shape_stable():
    from statementledger.frames import _statement

    stmt = _statement(Path(__file__).parent / "fixtures" / "bluelantern_savings.pdf")
    assert stmt["name"].endswith(".pdf")
    lines = stmt["lines"]
    assert lines[0]["items"][0][0] == "Blue Lantern Savings Bank"
    assert all(isinstance(i[1], int) and i[1] <= i[2]
               for line in lines for i in line["items"])
