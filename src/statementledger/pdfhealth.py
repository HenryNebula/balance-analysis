"""PDF triage before frames extraction: heal broken glyph encodings,
refuse scanned documents.

Two statement-PDF shapes read incorrectly (or not at all) with the default
extractor, and both are detectable from the file's own font structure:

1. HEALABLE — simple fonts whose /Encoding /Differences arrays name glyphs
   with literal uniXXXX names (the true text), while the font's /ToUnicode
   CMap is missing or WRONG. ToUnicode wins over the encoding in every
   compliant extractor, so a present-but-wrong map is exactly as damaging
   as a missing one: the default extraction outputs a consistent cipher or
   garbage while the page renders fine. heal() regenerates /ToUnicode from
   the /Differences names; after healing, every compliant extractor
   (pdfplumber, poppler, MuPDF) reads the true text. Geometry is untouched
   — only code->Unicode maps change — so overlay rectangles still align.

2. SCANNED — no text layer at all (the pages are images). No amount of
   encoding surgery recovers text from pixels; frames_for() raises
   ScannedPdfError so callers can say "OCR it first" instead of feeding a
   parser an empty layout.

Everything here is structural detection (pikepdf), never a guess from what
the extractor happened to output: a heal is armed only when the font's own
/Differences names disagree with (or outvote) its ToUnicode stream.
"""

from __future__ import annotations

import re
from pathlib import Path

# Minimal Adobe Glyph List fallback for non-uniXXXX names (the seen-in-the-
# wild arrays use uniXXXX exclusively; this covers the odd 'space').
AGL_MIN = {
    "space": 0x20, "period": 0x2E, "comma": 0x2C, "colon": 0x3A,
    "semicolon": 0x3B, "hyphen": 0x2D, "parenleft": 0x28, "parenright": 0x29,
    "dollar": 0x24, "asterisk": 0x2A, "ampersand": 0x26, "slash": 0x2F,
    "numbersign": 0x23, "bar": 0x7C, "plus": 0x2B, "minus": 0x2D,
}


class ScannedPdfError(Exception):
    """The PDF has no text layer — its pages are scanned images. No parser
    can read it as-is; run OCR (e.g. `ocrmypdf statement.pdf out.pdf`) and
    feed the OCR'd copy back in."""


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


# --- ToUnicode reading (agreement check) ----------------------------------------

BFCHAR_PAIR = re.compile(rb"<([0-9A-Fa-f]{1,4})>\s*<([0-9A-Fa-f]+)>")
BFRANGE_TRIPLE = re.compile(
    rb"<([0-9A-Fa-f]{1,4})>\s*<([0-9A-Fa-f]{1,4})>\s*<([0-9A-Fa-f]{4,})>")


def _utf16_first_point(hex_digits: str) -> int | None:
    """The first code point of a UTF-16BE hex string (<00430061> -> 0x43).
    Surrogate pairs collapse to their combined point; malformed tails and
    pure-BOM heads (xFEFF, an identity-UCS artifact) are handled."""
    if len(hex_digits) < 4:
        return None
    try:
        return int(hex_digits[:4], 16)
    except ValueError:
        return None


def parse_tounicode(data: bytes) -> dict[int, int]:
    """Flatten a ToUnicode CMap into {code: unicode point} — bfchar entries
    and the three-operand bfrange form (array-form ranges are not emitted
    by the producers this targets). First code point only: comparisons
    against uniXXXX names are single-point."""
    mapping: dict[int, int] = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for source, target in BFCHAR_PAIR.findall(block):
            point = _utf16_first_point(target.decode("ascii"))
            if point is not None:
                mapping[int(source, 16)] = point
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, base in BFRANGE_TRIPLE.findall(block):
            start, stop, origin = int(lo, 16), int(hi, 16), int(base, 16)
            if stop - start > 0xFFFF:
                continue  # absurd range: a misparsed block, not a map
            mapping.update(
                {code: origin + (code - start) for code in range(start, stop + 1)})
    return mapping


def _tounicode_of(font) -> dict[int, int] | None:
    """The font's existing ToUnicode map; None when the font carries no
    readable ToUnicode stream at all."""
    import pikepdf

    stream = font.get("/ToUnicode")
    if stream is None:
        return None
    try:
        return parse_tounicode(stream.read_bytes())
    except (AttributeError, ValueError):  # not a stream / unreadable
        return None


def healable_fonts(pdf: Path) -> list[dict]:
    """Fonts whose /Differences names can restore the true text but whose
    ToUnicode is missing or disagrees — [{base_font, codes, reason}].
    Empty list: the file's encodings are sane (or not our heal shape);
    extraction should proceed on the original bytes."""
    import pikepdf

    findings: list[dict] = []
    seen: set[int] = set()
    with pikepdf.open(pdf) as document:
        for obj in document.objects:  # walks indirect objects incl. font dicts
            try:
                if obj.get("/Type") != pikepdf.Name("/Font"):
                    continue
            except (AttributeError, TypeError):
                continue
            obj_id = obj.objgen if obj.is_indirect else id(obj)
            if obj_id in seen:
                continue
            seen.add(obj_id)
            try:
                encoding = obj.get("/Encoding")
            except (AttributeError, TypeError):
                continue
            if not isinstance(encoding, pikepdf.Dictionary):
                continue
            differences = encoding.get("/Differences")
            if differences is None:
                continue
            mapping = differences_to_map(differences)
            if not mapping:
                continue
            existing = _tounicode_of(obj)
            if existing is None:
                reason = "no ToUnicode"
            elif not existing:
                reason = "unreadable ToUnicode"
            elif any(existing.get(code) != point
                     for code, point in mapping.items()):
                reason = "ToUnicode disagrees with /Differences"
            else:
                continue  # already consistent: nothing to heal
            findings.append({"base_font": str(obj.get("/BaseFont", "?")),
                             "codes": len(mapping), "reason": reason})
    return findings


def heal(pdf_in: Path, pdf_out: Path) -> list[str]:
    """Rewrite /ToUnicode for every simple font with a /Differences array.

    Returns a per-font report. Existing ToUnicode streams are overwritten:
    a present-but-wrong map is exactly as damaging as a missing one (and is
    what confuses the better extractors into trusting it)."""
    import pikepdf

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
        try:
            encoding = obj.get("/Encoding")
        except (AttributeError, TypeError):
            continue
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
    pdf.close()
    return report


def has_images(pdf: Path) -> bool:
    """Any page places an image — the scanned-PDF tell (used only when the
    extractor found no text at all)."""
    import pdfplumber

    with pdfplumber.open(str(pdf)) as doc:
        return any(len(page.images) > 0 for page in doc.pages)
