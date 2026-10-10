"""The template-generated statement corpora (tools/templates/): visual
and hygiene gates for statements filled from real-world Word templates.

Three corpora live under data/gen/ (© TemplateLab-derived; template and
generated files stay in gitignored data/), so these tests SKIP on
machines without them — the committed proof of the pipelines lives in
docs/template-statement-visual-review.md (ui-review-loop, GA: YES for
each corpus).

Per corpus and across months, gated here:
- layout scan (tools/templates/layout_scan.py, per-template config):
  repeated headers, column x-invariance, footers/page markers, fixed
  page counts, no ink-level rule crossings (beyond template-authentic
  allowlists)
- truth ledgers reconcile and chain month to month
- no registered parser FALSE-POSITIVELY verifies these statements
- regeneration is deterministic (same seed -> same sha256)
- re-rendered pages are pixel-identical to the tour baselines

Template-specific:
- tl-44769: T&C present/identical/last; narration variation axes
  present; every narration word survives into the text layer
- tl-44773: the five printed tables AGREE with each other (summary vs
  section totals vs daily balance vs fee counters), read from the PDF
  text like a parser would
- tl-44745: activity-grid subtotals sum to the invoice's Current
  Service Charge, and Total Due carries through every printed copy
"""

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import statementledger as sl

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "data" / "gen"

CORPORA = sorted(p.parent for p in GEN.glob("*/manifest.jsonl"))
IDS = [c.name for c in CORPORA]
pytestmark = pytest.mark.skipif(
    not CORPORA, reason="no template corpora generated locally "
                        "(data/ is machine-local)")


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "layout_scan", ROOT / "tools" / "templates" / "layout_scan.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def manifest(corpus: Path) -> list[dict]:
    return [json.loads(line) for line in
            (corpus / "manifest.jsonl").read_text().splitlines() if line.strip()]


def truth(corpus: Path, row: dict) -> dict:
    return json.loads((corpus / "truth" /
                       f"{row['month']}.truth.json").read_text())


def page_text(corpus: Path, row: dict, page: int) -> str:
    # -layout: keeps table columns and positioned boxes on their lines
    out = subprocess.run(["pdftotext", "-layout", "-f", str(page),
                          "-l", str(page), str(corpus / row["file"]), "-"],
                         capture_output=True, text=True, check=True)
    return out.stdout


def doc_text(corpus: Path, row: dict) -> str:
    # -layout keeps positioned textboxes on their visual lines; raw mode
    # interleaves them in stream order
    out = subprocess.run(["pdftotext", "-layout", str(corpus / row["file"]),
                          "-"], capture_output=True, text=True, check=True)
    return out.stdout


# --- gates that apply to every corpus ---------------------------------------

@pytest.mark.parametrize("corpus", CORPORA, ids=IDS)
def test_layout_scan_clean(corpus):
    assert _load_scan().main(corpus) == 0


@pytest.mark.parametrize("corpus", CORPORA, ids=IDS)
@pytest.mark.parametrize("row_index", range(12))
def test_truth_ledger_reconciles(corpus, row_index):
    rows = manifest(corpus)
    if row_index >= len(rows):
        pytest.skip("short corpus")
    assert sl.reconcile_statement(truth(corpus, rows[row_index])) is None


@pytest.mark.parametrize("corpus", CORPORA, ids=IDS)
def test_balances_chain_across_months(corpus):
    rows = manifest(corpus)
    for prev, nxt in zip(rows, rows[1:]):
        assert prev["closing"] == nxt["opening"], (prev["month"],
                                                   nxt["month"])


@pytest.mark.parametrize("corpus", CORPORA, ids=IDS)
@pytest.mark.parametrize("row_index", range(12))
def test_registry_behaviour_on_corpus(corpus, row_index):
    """The ledger corpus (tl-44769, fictional bank) must verify under NO
    registered parser. The Chase-family corpora are different: the
    existing Chase parsers were distilled from same-family layouts, so a
    verify is EXPECTED — but "verified" is necessary, not sufficient:
    the parser may still differ from ground truth in presentation
    semantics (print-order vs chronological rows; computed balances;
    check-number descriptions; the billing parser books the previous
    invoice as a transaction with opening 0). Gate the honest part: any
    verifying ledger must contain every truth transaction (by date and
    amount) and agree on the closing balance."""
    rows = manifest(corpus)
    if row_index >= len(rows):
        pytest.skip("short corpus")
    row = rows[row_index]
    t = truth(corpus, row)
    pdf = str(corpus / row["file"])
    if corpus.name == "tl-44769":
        with pytest.raises(sl.VerificationError):
            sl.parse(pdf)
        return
    try:
        got = sl.parse(pdf)
    except sl.VerificationError:
        return                      # no parser claims it: also fine
    if isinstance(got, list):
        got = got[0]
    assert abs(got["closing_balance"] - t["closing_balance"]) < 0.005
    truth_keys = sorted((x["date"], round(x["amount"], 2))
                        for x in t["transactions"])
    got_keys = sorted((x["date"], round(x["amount"], 2))
                      for x in got["transactions"])
    missing = [k for k in truth_keys if k not in got_keys]
    assert not missing, f"verifying parser lost transactions: {missing}"


@pytest.mark.parametrize("corpus", CORPORA, ids=IDS)
def test_manifest_matches_truth_counts(corpus):
    for row in manifest(corpus):
        assert len(truth(corpus, row)["transactions"]) == row["transactions"]


@pytest.mark.skipif(shutil.which("soffice") is None, reason="needs soffice")
@pytest.mark.parametrize("corpus", CORPORA, ids=IDS)
def test_regeneration_is_deterministic(corpus, tmp_path):
    """Same template + seed -> identical PDF bytes (metadata normalized)."""
    import hashlib
    rows = manifest(corpus)
    row = rows[0]
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "templates" /
                             "gen_statements.py"),
         "--template", row["template"], "--year", row["month"][:4],
         "--months", "1", "--min-txns", "10", "--max-txns", "85",
         "--seed", "meridian", "--out", str(tmp_path)],
        check=True, capture_output=True, cwd=ROOT)
    regen = tmp_path / row["template"] / "pdf" / f"{row['month']}.pdf"
    assert (hashlib.sha256(regen.read_bytes()).hexdigest()
            == row["sha256"]), "regenerated PDF differs from manifest pin"


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="needs pdftoppm")
@pytest.mark.parametrize("corpus", CORPORA, ids=IDS)
def test_rendered_pages_match_tour_baselines(corpus, tmp_path):
    """UI-style golden test: re-render and pixel-compare the tour pages."""
    from PIL import Image, ImageChops
    tour = corpus / "tour"
    if not tour.exists():
        pytest.skip("tour baselines not rendered (run render_tour.py)")
    rows = {r["month"]: r for r in manifest(corpus)}
    checked = 0
    for png in sorted(tour.glob("*.png")):
        month, page = png.stem.rsplit("-", 1)
        subprocess.run(["pdftoppm", "-r", "100", "-png", "-f", page,
                        "-l", page, str(corpus / rows[month]["file"]),
                        str(tmp_path / png.stem)], check=True)
        fresh = tmp_path / f"{png.stem}-{page}.png"
        a, b = Image.open(png).convert("L"), Image.open(fresh).convert("L")
        assert a.size == b.size, png.name
        assert ImageChops.difference(a, b).getbbox() is None, png.name
        checked += 1
    assert checked >= 6, "expected a real tour in the corpus"


# --- tl-44769: ledger statement with T&C and presentation variations -------

@pytest.fixture()
def c44769():
    c = GEN / "tl-44769"
    if not (c / "manifest.jsonl").exists():
        pytest.skip("tl-44769 corpus not generated")
    return c


def test_terms_and_conditions_present_uniform_and_last(c44769):
    texts = []
    for row in manifest(c44769):
        last = page_text(c44769, row, row["pages"])
        assert "TERMS AND CONDITIONS" in last, row["month"]
        assert "END OF STATEMENT" not in last
        texts.append(re.sub(r"Page \d+ of \d+", "Page N of M", last))
    assert len(set(texts)) == 1, "T&C must be byte-identical across months"


def test_variation_scenarios_present(c44769):
    v = [sum(r["variation"][k] for r in manifest(c44769)) for k in
         ("threeline_txns", "chq_filled", "blank_money_cells",
          "unprinted_balances")]
    assert v[0] >= 1 and v[1] >= 5 and v[2] >= 5 and v[3] >= 5


def test_statement_lengths_vary(c44769):
    rows = manifest(c44769)
    assert len({r["pages"] for r in rows}) > 1
    assert len({r["transactions"] for r in rows}) > 3


@pytest.mark.parametrize("row_index", range(12))
def test_every_narration_line_survives_rendering(c44769, row_index):
    rows = manifest(c44769)
    if row_index >= len(rows):
        pytest.skip("short corpus")
    row = rows[row_index]
    wordset = set(doc_text(c44769, row).split())
    for txn in truth(c44769, row)["transactions"]:
        missing = [w for w in txn["description"].split() if w not in wordset]
        assert not missing, (row["month"], txn["description"], missing)


# --- tl-44773: five printed tables must agree with each other --------------

@pytest.fixture()
def c44773():
    c = GEN / "tl-44773"
    if not (c / "manifest.jsonl").exists():
        pytest.skip("tl-44773 corpus not generated")
    return c


def _amount(pattern: str, text: str) -> float:
    m = re.search(pattern, text)
    assert m, pattern
    return float(m.group(1).replace(",", ""))


@pytest.mark.parametrize("row_index", range(12))
def test_printed_tables_agree(c44773, row_index):
    """Read the statement like a parser: summary vs section totals vs
    daily balance vs fee counters must reconcile from the printed text."""
    rows = manifest(c44773)
    if row_index >= len(rows):
        pytest.skip("short corpus")
    row = rows[row_index]
    p1 = page_text(c44773, row, 1)
    p3 = page_text(c44773, row, 3)

    beginning = _amount(r"Beginning Balance\s*\$([\d,]+\.\d{2})", p1)
    dep_total = _amount(
        r"Total Deposits and Additions\s*\$?([\d,]+\.\d{2})", p1)
    chk_total = _amount(r"Total Checks Paid\s*\$([\d,]+\.\d{2})", p3)
    wdr_total = _amount(
        r"Total Other Withdrawals, Fees & Charges\s*\$([\d,]+\.\d{2})", p3)
    ending = _amount(r"Ending Balance\s*\d+\s*\$([\d,]+\.\d{2})", p1)

    assert abs(beginning + dep_total - chk_total - wdr_total
               - ending) < 0.005
    # the daily balance series ends at the summary's ending balance
    daily_section = p3.split("DAILY ENDING BALANCE", 1)[1] \
                      .split("SERVICE CHARGE", 1)[0]
    pairs = re.findall(r"(\d{2}/\d{2})\s+\$?([\d,]+\.\d{2})", daily_section)
    assert pairs, "no daily balance pairs found"
    daily_last = float(pairs[-1][1].replace(",", ""))
    assert abs(daily_last - ending) < 0.005
    # fee counters: debits + credits == transaction total
    debits = int(re.search(r"Checks Paid / Debits\s+(\d+)", p3).group(1))
    credits = int(re.search(r"Deposits / Credits\s+(\d+)", p3).group(1))
    total_n = int(re.search(r"Transaction Total\s+(\d+)", p3).group(1))
    assert debits + credits == total_n
    # truth agrees with the printed ending balance
    assert abs(truth(c44773, row)["closing_balance"] - ending) < 0.005


def test_chase_variation_present(c44773):
    v = [sum(r["variation"][k] for r in manifest(c44773)) for k in
         ("checks", "wrapped_withdrawals", "daily_balance_points")]
    assert v[0] >= 12 and v[1] >= 5 and v[2] >= 24


# --- tl-44745: billing invoice arithmetic -----------------------------------

@pytest.fixture()
def c44745():
    c = GEN / "tl-44745"
    if not (c / "manifest.jsonl").exists():
        pytest.skip("tl-44745 corpus not generated")
    return c


@pytest.mark.parametrize("row_index", range(12))
def test_billing_arithmetic_printed(c44745, row_index):
    """Grid subtotals sum to the invoice charge; every printed Total Due
    copy agrees; the payment zeroes the previous due."""
    rows = manifest(c44745)
    if row_index >= len(rows):
        pytest.skip("short corpus")
    row = rows[row_index]
    doc = doc_text(c44745, row)

    # service rows print one per line: code, description, volume, curr,
    # unit price, charge (the note cells interleave but never start a
    # line with a 4-digit code)
    row_charges = [float(m.group(1)) for m in re.finditer(
        r"^\s+\d{4}\s+[A-Z].*?\s\d+\s+USD\s+[\d.]+\s+([\d.]+)\s*$",
        doc, re.M)]
    total_activity = _amount(
        r"Total Activity Charge\s+USD\s+([\d,]+\.\d{2})", doc)
    assert len(row_charges) >= 10, row_charges
    assert abs(sum(row_charges) - total_activity) < 0.005

    current = _amount(r"Current Service Charge\s+\d{2} \w{3} \d{4}"
                      r"\s+([\d,]+\.\d{2})", doc)
    assert abs(current - total_activity) < 0.005, \
        "grid subtotals must equal the invoice charge"

    dues = [float(d.replace(",", "")) for pair in re.findall(
        r"Total Due CAD\s+([\d,]+\.\d{2})|"
        r"Total Amount Due \(CAD\)\s+([\d,]+\.\d{2})", doc)
        for d in pair if d]
    assert dues and all(abs(d - current) < 0.005 for d in dues), dues
    assert abs(truth(c44745, row)["closing_balance"] - current) < 0.005


def test_billing_chains_and_shape(c44745):
    rows = manifest(c44745)
    assert all(r["pages"] == 6 and r["transactions"] == 2 for r in rows)
    # every month pays the previous invoice in full
    for prev, nxt in zip(rows, rows[1:]):
        assert nxt["opening"] == prev["closing"]
