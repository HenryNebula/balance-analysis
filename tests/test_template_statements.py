"""The template-generated statement corpus (tools/templates/): the visual
and hygiene gates for statements filled from a real-world Word template.

The corpus under data/gen/ is © TemplateLab-derived (template + generated
PDFs stay in gitignored data/), so these tests SKIP on machines without
it — the committed proof of the pipeline lives in
docs/template-statement-visual-review.md (5-round ui-review-loop, GA: YES).

What is gated here, per corpus statement and across months:
- layout scan (tools/templates/layout_scan.py): repeated headers, column
  x-invariance, footer "Page N of M", no ink-level rule collisions
- the generator's truth ledgers reconcile and chain month to month
- statement lengths vary (the scenario the corpus exists for)
- T&C present, uniform, on the final page of every statement
- no registered parser FALSE-POSITIVELY verifies these statements
- regeneration is deterministic (same seed -> same sha256) when the
  local toolchain (soffice) is present
- re-rendered pages are pixel-identical to the tour baselines when
  pdftoppm is present (the UI-style golden test)
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import statementledger as sl

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "gen" / "tl-44769"

pytestmark = pytest.mark.skipif(
    not (CORPUS / "manifest.jsonl").exists(),
    reason="template corpus not generated locally (data/ is machine-local)")


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "layout_scan", ROOT / "tools" / "templates" / "layout_scan.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def manifest() -> list[dict]:
    return [json.loads(line) for line in
            (CORPUS / "manifest.jsonl").read_text().splitlines() if line.strip()]


def truth(row: dict) -> dict:
    return json.loads((CORPUS / "truth" /
                       f"{row['month']}.truth.json").read_text())


def test_layout_scan_clean():
    assert _load_scan().main(CORPUS) == 0


@pytest.mark.parametrize("row", manifest(), ids=lambda r: r["month"])
def test_truth_ledger_reconciles(row):
    assert sl.reconcile_statement(truth(row)) is None


def test_balances_chain_across_months():
    rows = manifest()
    for prev, nxt in zip(rows, rows[1:]):
        assert prev["closing"] == nxt["opening"], (prev["month"], nxt["month"])


def test_statement_lengths_vary():
    rows = manifest()
    assert len({r["pages"] for r in rows}) > 1, "page counts should vary"
    assert len({r["transactions"] for r in rows}) > 3, \
        "transaction counts should vary"


def test_manifest_matches_truth_counts():
    for row in manifest():
        assert len(truth(row)["transactions"]) == row["transactions"]


def _page_text(pdf: Path, page: int) -> str:
    out = subprocess.run(["pdftotext", "-f", str(page), "-l", str(page),
                          str(pdf), "-"], capture_output=True, text=True,
                         check=True)
    return out.stdout


def test_terms_and_conditions_present_uniform_and_last():
    import re
    texts = []
    for row in manifest():
        last = _page_text(CORPUS / row["file"], row["pages"])
        assert "TERMS AND CONDITIONS" in last, row["month"]
        assert "END OF STATEMENT" not in last  # T&C owns the final page
        # normalize the page counter away, then require identical terms
        texts.append(re.sub(r"Page \d+ of \d+", "Page N of M", last))
    assert len(set(texts)) == 1, "T&C must be byte-identical across months"


@pytest.mark.parametrize("row", manifest(), ids=lambda r: r["month"])
def test_no_registered_parser_false_positives(row):
    with pytest.raises(sl.VerificationError):
        sl.parse(str(CORPUS / row["file"]))


@pytest.mark.skipif(shutil.which("soffice") is None, reason="needs soffice")
def test_regeneration_is_deterministic(tmp_path):
    """Same template + seed -> identical PDF bytes (metadata normalized)."""
    import hashlib
    rows = manifest()
    row = rows[0]
    env = dict(**{"UV_CACHE_DIR": "/media/ext4-data/cache/uv-cache",
                  "UV_PROJECT_ENVIRONMENT":
                      "/media/ext4-data/venvs/balance-analysis"})
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "templates" /
                             "gen_statements.py"),
         "--template", row["template"], "--year", row["month"][:4],
         "--months", "1", "--min-txns", "10", "--max-txns", "85",
         "--seed", "meridian", "--out", str(tmp_path)],
        check=True, capture_output=True, env=env, cwd=ROOT)
    # reproduce the FULL chain: month 1 opens the chain, so a single-month
    # regen must equal the corpus month 1
    regen = tmp_path / row["template"] / "pdf" / f"{row['month']}.pdf"
    assert (hashlib.sha256(regen.read_bytes()).hexdigest()
            == row["sha256"]), "regenerated PDF differs from manifest pin"


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="needs pdftoppm")
def test_rendered_pages_match_tour_baselines(tmp_path):
    """UI-style golden test: re-render and pixel-compare the tour pages."""
    from PIL import Image, ImageChops
    tour = CORPUS / "tour"
    if not tour.exists():
        pytest.skip("tour baselines not rendered (run render_tour.py)")
    rows = {r["month"]: r for r in manifest()}
    checked = 0
    for png in sorted(tour.glob("*.png")):
        month, page = png.stem.rsplit("-", 1)
        subprocess.run(["pdftoppm", "-r", "100", "-png", "-f", page,
                        "-l", page, str(CORPUS / rows[month]["file"]),
                        str(tmp_path / png.stem)], check=True)
        fresh = tmp_path / f"{png.stem}-{page}.png"
        a, b = Image.open(png).convert("L"), Image.open(fresh).convert("L")
        assert a.size == b.size, png.name
        assert ImageChops.difference(a, b).getbbox() is None, png.name
        checked += 1
    assert checked >= 12, "expected at least the T&C pages in the tour"
