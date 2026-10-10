"""Structural layout scan over a generated corpus — the objective twin of
the agy visual review. Checks every PDF for:

- repeated table header on every page >= 2 (same column x-centers)
- column x-centers identical across pages AND across months
- no word bbox intersecting a horizontal rule (the round-1 collision)
- footer "Page N of M" correct on every page

    uv run python tools/templates/layout_scan.py data/gen/tl-44769
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pdfplumber

HEADER_WORDS = ("DATE", "PARTICULARS", "CHQ.NO", "WITHDRAW", "DEPOSIT",
                "BALANCE")


def header_centers(page) -> dict[str, float] | None:
    words = {w["text"]: (w["x0"] + w["x1"]) / 2 for w in page.extract_words()}
    if not all(h in words for h in HEADER_WORDS):
        return None
    return {h: words[h] for h in HEADER_WORDS}


def page_has_table_body(page, date_x: float) -> bool:
    """A page carries table body content iff a dd-Mon-yyyy date sits in
    the DATE column (continuation pages must then repeat the header)."""
    return any(re.fullmatch(r"\d{2}-[A-Z][a-z]{2}-\d{4}", w["text"])
               and abs(w["x0"] - date_x) < 3
               for w in page.extract_words())


def scan(pdf_path: Path) -> list[str]:
    problems = []
    with pdfplumber.open(pdf_path) as pdf:
        # DATE column x from any page that has the header
        date_x = None
        for page in pdf.pages:
            hc = header_centers(page)
            if hc:
                words = {w["text"]: w["x0"] for w in page.extract_words()}
                date_x = words["DATE"]
                break
        centers = []
        for i, page in enumerate(pdf.pages, start=1):
            hc = header_centers(page)
            if i >= 2 and date_x is not None and hc is None \
                    and page_has_table_body(page, date_x):
                problems.append(f"p{i}: repeated table header missing")
            if hc:
                centers.append(hc)
            # words crossing horizontal rules — through actual INK, not the
            # glyph box: at 8pt Courier the ascender box starts ~2pt above
            # digit ink, and the row-boundary rules sit inside that gap
            hlines = [l for l in page.lines + page.rects
                      if abs(l["y0"] - l["y1"]) < 1 and
                      (l["x1"] - l["x0"]) > 100]
            for w in page.extract_words():
                for l in hlines:
                    if (w["top"] + 2.2 < l["top"] < w["bottom"] - 0.5 and
                            w["x0"] < l["x1"] and w["x1"] > l["x0"]):
                        problems.append(
                            f"p{i}: word {w['text']!r} crosses rule at "
                            f"y={l['top']:.1f}")
                        break
            # footer (template textbox sits ~85pt above the page bottom)
            footer = " ".join(w["text"] for w in page.extract_words()
                              if w["top"] > page.height - 110)
            expect = f"Page {i} of {len(pdf.pages)}"
            if expect not in footer:
                problems.append(f"p{i}: footer {footer!r} != {expect!r}")
        # cross-page column consistency
        if centers:
            ref = centers[0]
            for i, c in enumerate(centers, start=1):
                for col, x in ref.items():
                    if abs(c[col] - x) > 0.5:
                        problems.append(
                            f"p{i}: column {col} shifted "
                            f"{c[col]:.1f} vs {x:.1f}")
    return problems


def main(corpus: Path | str | None = None) -> int:
    corpus = Path(corpus or sys.argv[1])
    manifest = [json.loads(line) for line in
                (corpus / "manifest.jsonl").read_text().splitlines() if line.strip()]
    all_centers: dict[str, dict[str, float]] = {}
    failures = 0
    for row in manifest:
        problems = scan(corpus / row["file"])
        with pdfplumber.open(corpus / row["file"]) as pdf:
            hc = None
            for page in pdf.pages:
                hc = header_centers(page) or hc
        if hc:
            all_centers[row["month"]] = hc
        if problems:
            failures += 1
            print(f"{row['month']}: {len(problems)} problems")
            for p in problems[:8]:
                print("   ", p)
    # cross-month column consistency
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
