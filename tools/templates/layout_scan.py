"""Structural layout scan over a generated corpus — the objective twin of
the agy visual review. Checks every PDF for:

- repeated table header on table-body continuation pages (templates with
  page-spanning tables), same column x-centers
- column x-centers identical across pages AND across months
- no word bbox intersecting a horizontal rule through its ink
- footer "Page N of M" correct on every page (where the template prints
  one), and fixed page counts where the template pins them

Per-template behaviour comes from CONFIG below (keyed by template id in
the corpus manifest).

    uv run python tools/templates/layout_scan.py data/gen/tl-44769
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pdfplumber

# tl-44769: one ledger table, dd-Mon-yyyy dates, footer textbox.
# tl-44773: five single-page sections, MM/DD dates, literal 'Page N of 4'
#           paragraphs, exactly 4 pages.
# tl-44745: fixed 6-page landscape billing package, 'PAGE 2 OF 6' literal,
#           no repeating table header to check.
CONFIG = {
    "tl-44769": {
        "header_words": ("DATE", "PARTICULARS", "CHQ.NO", "WITHDRAW",
                         "DEPOSIT", "BALANCE"),
        "date_re": r"\d{2}-[A-Z][a-z]{2}-\d{4}",
        "footer": "bottom",          # 'Page N of M' near the page bottom
        "expect_pages": None,
    },
    "tl-44773": {
        "header_words": ("DATE", "DESCRIPTION", "AMOUNT"),
        "date_re": r"\d{2}/\d{2}",
        "footer": "any",             # literal body paragraph
        "expect_pages": 4,
        "cross_page_columns": False, # each page is a DIFFERENT table
    },
    "tl-44745": {
        "header_words": None,
        "date_re": None,
        "footer": "page2",           # only 'PAGE 2 OF 6' is printed
        "expect_pages": 6,
        # the definitions page's rule passes through a text line in the
        # template's own PDF — template-authentic, not a fill artifact
        "crossing_allowlist": {("quarter", 89.6), ("-end", 89.6),
                               ("months.", 89.6)},
    },
}


def header_centers(page, words: tuple) -> dict[str, float] | None:
    ws = {w["text"]: (w["x0"] + w["x1"]) / 2 for w in page.extract_words()}
    if not all(h in ws for h in words):
        return None
    return {h: ws[h] for h in words}


def page_has_table_body(page, date_x: float, date_re: str) -> bool:
    """A page carries table body content iff a date sits in the DATE
    column (continuation pages must then repeat the header)."""
    return any(re.fullmatch(date_re, w["text"])
               and abs(w["x0"] - date_x) < 3
               for w in page.extract_words())


def scan(pdf_path: Path, cfg: dict) -> list[str]:
    problems = []
    with pdfplumber.open(pdf_path) as pdf:
        if cfg["expect_pages"] and len(pdf.pages) != cfg["expect_pages"]:
            problems.append(f"expected {cfg['expect_pages']} pages, "
                            f"got {len(pdf.pages)}")
        words_tuple = cfg["header_words"]
        date_x = None
        if words_tuple:
            for page in pdf.pages:
                hc = header_centers(page, words_tuple)
                if hc:
                    ws = {w["text"]: w["x0"] for w in page.extract_words()}
                    date_x = ws[words_tuple[0]]
                    break
        centers = []
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if words_tuple:
                hc = header_centers(page, words_tuple)
                if i >= 2 and date_x is not None and hc is None \
                        and page_has_table_body(page, date_x, cfg["date_re"]):
                    problems.append(f"p{i}: repeated table header missing")
                if hc:
                    centers.append(hc)
            # words crossing horizontal rules — through actual INK, not the
            # glyph box: at 8pt Courier the ascender box starts ~2pt above
            # digit ink, and the row-boundary rules sit inside that gap
            hlines = [l for l in page.lines + page.rects
                      if abs(l["y0"] - l["y1"]) < 1 and
                      (l["x1"] - l["x0"]) > 100]
            allow = cfg.get("crossing_allowlist", set())
            for w in page.extract_words():
                for l in hlines:
                    if (w["top"] + 2.2 < l["top"] < w["bottom"] - 0.5 and
                            w["x0"] < l["x1"] and w["x1"] > l["x0"]):
                        if (w["text"], round(l["top"], 1)) in allow:
                            continue
                        problems.append(
                            f"p{i}: word {w['text']!r} crosses rule at "
                            f"y={l['top']:.1f}")
                        break
            # footer 'Page N of M'
            expect = f"Page {i} of {len(pdf.pages)}"
            if cfg["footer"] == "bottom":
                footer = " ".join(w["text"] for w in page.extract_words()
                                  if w["top"] > page.height - 110)
                if expect not in footer:
                    problems.append(f"p{i}: footer {footer!r} != {expect!r}")
            elif cfg["footer"] == "any":
                up = text.upper()
                if f"PAGE {i} OF {len(pdf.pages)}" not in up:
                    problems.append(f"p{i}: page marker {expect!r} missing")
            elif cfg["footer"] == "page2" and i == 2:
                if f"PAGE 2 OF {len(pdf.pages)}" not in text.upper():
                    problems.append("p2: 'PAGE 2 OF N' marker missing")
        if centers and len(centers) > 1 and cfg.get("cross_page_columns",
                                                     True):
            ref = centers[0]
            for i, c in enumerate(centers, start=1):
                for col, x in ref.items():
                    if abs(c[col] - x) > 0.5:
                        problems.append(
                            f"p{i}: column {col} shifted {c[col]:.1f} vs "
                            f"{x:.1f}")
    return problems


def main(corpus: Path | str | None = None) -> int:
    corpus = Path(corpus or sys.argv[1])
    manifest = [json.loads(line) for line in
                (corpus / "manifest.jsonl").read_text().splitlines() if line.strip()]
    cfg = CONFIG[manifest[0]["template"]]
    all_centers: dict[str, dict[str, float]] = {}
    failures = 0
    for row in manifest:
        problems = scan(corpus / row["file"], cfg)
        if cfg["header_words"]:
            with pdfplumber.open(corpus / row["file"]) as pdf:
                hc = None
                for page in pdf.pages:
                    # first page only for multi-table statements: cross-
                    # month consistency compares the same table
                    hc = header_centers(page, cfg["header_words"]) or hc
                    if hc:
                        break
            if hc:
                all_centers[row["month"]] = hc
        if problems:
            failures += 1
            print(f"{row['month']}: {len(problems)} problems")
            for p in problems[:8]:
                print("   ", p)
    # cross-month column consistency
    if all_centers:
        ref_month = next(iter(all_centers))
        ref = all_centers[ref_month]
        for month, c in all_centers.items():
            for col, x in ref.items():
                if abs(c[col] - x) > 0.5:
                    print(f"{month}: column {col} differs from {ref_month}: "
                          f"{c[col]:.1f} vs {x[col]:.1f}")
                    failures += 1
    print(f"scanned {len(manifest)} statements, "
          f"{'FAIL' if failures else 'OK'} ({failures} statement-level failures)")
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
