"""Heal PDFs whose fonts carry broken/absent ToUnicode maps (CLI wrapper).

The healing and detection logic lives in statementledger.pdfhealth (it is
part of every frames_for() extraction now); this script is the standalone
form for pre-inspecting a file:

    uv run python heal_pdf.py statement.pdf statement_healed.pdf
    # optional self-check: extract a sample line from the healed copy
    uv run python heal_pdf.py statement.pdf statement_healed.pdf --check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from statementledger.pdfhealth import heal, healable_fonts


def check(pdf_out: Path, chars: int = 400) -> None:
    """Best-effort extraction sample from the healed copy (needs pymupdf)."""
    try:
        import pymupdf
    except ImportError:
        print("(pymupdf not installed — skipping --check sample)")
        return
    doc = pymupdf.open(pdf_out)
    print(f"\n--- healed text sample (page 1, first {chars} chars) ---")
    print(doc[0].get_text()[:chars])


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("pdf_in", type=Path)
    cli.add_argument("pdf_out", type=Path)
    cli.add_argument("--check", action="store_true", help="print a text sample after healing")
    args = cli.parse_args()

    if not args.pdf_out.exists() and args.pdf_out == args.pdf_in:
        print("output path must differ from input")
        return 1
    findings = healable_fonts(args.pdf_in)
    if not findings:
        print("no healable fonts found — nothing to heal (file may already be sane)")
        return 0
    for finding in findings:
        print(f"{finding['base_font']}: {finding['codes']} codes — "
              f"{finding['reason']}")
    for line in heal(args.pdf_in, args.pdf_out):
        print(line)
    print(f"\nhealed: {args.pdf_out}")
    if args.check:
        check(args.pdf_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
