"""Heal PDFs whose fonts carry broken/absent ToUnicode maps.

Targets the statement-generator pattern seen in the wild: Type1 subset
fonts whose /Encoding /Differences arrays name glyphs with literal uniXXXX
names (assigned in order of first use), while the PDF-level ToUnicode
streams are missing or wrong. Extractors that cannot walk that chain output
raw codes (a consistent cipher) or garbage; rendering is unaffected.

For every simple font with a /Differences array this tool derives
code -> uniXXXX -> Unicode and REGENERATES the font's /ToUnicode CMap
(overwriting any existing — wrong — stream). After healing, every
compliant extractor (pdf-rs in the fork, poppler, MuPDF) reads the true
text, and the rules-engine fast path works on the healed file.

Usage (from balance-analysis/):
    uv run python heal_pdf.py statement.pdf statement_healed.pdf
    # optional self-check: extract a sample line from the healed copy
    uv run python heal_pdf.py statement.pdf statement_healed.pdf --check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pikepdf

# Minimal Adobe Glyph List fallback for non-uniXXXX names (the seen-in-the-
# wild arrays use uniXXXX exclusively; this covers the odd 'space').
AGL_MIN = {
    "space": 0x20, "period": 0x2E, "comma": 0x2C, "colon": 0x3A,
    "semicolon": 0x3B, "hyphen": 0x2D, "parenleft": 0x28, "parenright": 0x29,
    "dollar": 0x24, "asterisk": 0x2A, "ampersand": 0x26, "slash": 0x2F,
    "numbersign": 0x23, "bar": 0x7C, "plus": 0x2B, "minus": 0x2D,
}


def glyph_name_to_unicode(name: str) -> int | None:
    """uniXXXX (optionally two code points g-prefixed) per the CMap spec,
    then a tiny AGL subset. Returns None for names we cannot resolve —
    the entry is then skipped rather than guessed."""
    if name.startswith("uni") and len(name) >= 7:
        try:
            return int(name[3:7], 16)
        except ValueError:
            return None
    if len(name) == 2 and name.startswith("g"):
        try:
            return int(name[1:], 16)
        except ValueError:
            return None
    return AGL_MIN.get(name)


def differences_to_map(differences) -> dict[int, int]:
    """Flatten a /Differences array into {char code: unicode point}.
    Entries look like [33 /uni0043 /uni0075 ...]: a code resets the
    position, names increment it. Unresolvable names are skipped."""
    mapping: dict[int, int] = {}
    code = 0
    for item in differences:
        if isinstance(item, (int, float)):  # pikepdf passes ints; floats guard
            code = int(item)
            continue
        name = str(item).lstrip("/")
        point = glyph_name_to_unicode(name)
        if point is not None:
            mapping[code] = point
        code += 1
    return mapping


def to_cmap_stream(mapping: dict[int, int]) -> bytes:
    """A minimal ToUnicode CMap with bfchar entries (max 100 per block)."""
    entries = sorted(mapping.items())
    blocks = []
    for start in range(0, len(entries), 100):
        chunk = entries[start : start + 100]
        lines = [f"{len(chunk)} beginbfchar"]
        lines += [f"<{code:02X}> <{point:04X}>" for code, point in chunk]
        lines.append("endbfchar")
        blocks.append("\n".join(lines))
    body = "\n".join(blocks)
    return (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\n"
        "begincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n"
        "/CMapType 2 def\n"
        "1 begincodespacerange\n"
        "<00> <FF>\n"
        "endcodespacerange\n"
        f"{body}\n"
        "endcmap\n"
        "CMapName currentdict /CMap defineresource pop\n"
        "end\n"
        "end\n"
    ).encode("ascii")


def heal(pdf_in: Path, pdf_out: Path) -> list[str]:
    """Rewrite /ToUnicode for every simple font with a /Differences array.

    Returns a per-font report. Existing ToUnicode streams are overwritten:
    a present-but-wrong map is exactly as damaging as a missing one (and is
    what confuses the better extractors into trusting it)."""
    pdf = pikepdf.open(pdf_in)
    report: list[str] = []
    seen: set[int] = set()
    for obj in pdf.objects:  # walks indirect objects incl. font dicts
        try:
            if obj.get("/Type") != pikepdf.Name("/Font"):
                continue
        except (AttributeError, TypeError):
            continue
        obj_id = obj.unparse() if not obj.is_indirect else obj.objgen
        if obj_id in seen:
            continue
        seen.add(obj_id)
        encoding = obj.get("/Encoding")
        if not isinstance(encoding, pikepdf.Dictionary):
            continue
        differences = encoding.get("/Differences")
        if differences is None:
            continue
        mapping = differences_to_map(differences)
        if not mapping:
            continue
        base = str(obj.get("/BaseFont", "?"))
        stream = pikepdf.Stream(pdf, to_cmap_stream(mapping))
        obj.ToUnicode = stream
        sample = "".join(chr(mapping[c]) for c in sorted(mapping)[:12])
        report.append(f"{base}: {len(mapping)} codes -> ToUnicode (starts {sample!r})")
    pdf.save(pdf_out)
    return report


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
    report = heal(args.pdf_in, args.pdf_out)
    if not report:
        print("no /Differences fonts found — nothing to heal (file may already be sane)")
        if not args.pdf_out.exists():
            return 1
        return 0
    for line in report:
        print(line)
    print(f"\nhealed: {args.pdf_out}")
    if args.check:
        check(args.pdf_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
