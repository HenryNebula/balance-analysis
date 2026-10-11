"""PDF triage: broken-glyph healing and scanned-PDF detection, end to end
with PDFs built in-test (reportlab) and corrupted by hand (pikepdf)."""

import json
from pathlib import Path

import pytest

from statementledger import ScannedPdfError
from statementledger.frames import frames_for
from statementledger.pdfhealth import (differences_to_map, heal,
                                       healable_fonts, parse_tounicode,
                                       to_cmap_stream)

TEXT = "Account number: 1234 5678"


# --- pure map machinery -----------------------------------------------------------


def test_differences_to_map_resolves_uni_names_and_resets():
    mapping = differences_to_map([33, "/uni0043", "/uni0075", "/uni0072",
                                  47, "/period", "/nosuchglyph"])
    assert mapping == {33: ord("C"), 34: ord("u"), 35: ord("r"),
                       47: ord(".")}
    # unresolvable names are skipped, not guessed; AGL fallback covers space
    assert differences_to_map([32, "/space"]) == {32: 0x20}


def test_cmap_roundtrip_and_bfrange():
    mapping = {33: 0x43, 34: 0x75, 35: 0x72}
    assert parse_tounicode(to_cmap_stream(mapping)) == mapping
    # the three-operand bfrange form
    cmap = (b"1 beginbfrange\n<20> <25> <0041>\nendbfrange\n")
    assert parse_tounicode(cmap) == {c: 0x41 + c - 0x20 for c in range(0x20, 0x26)}


# --- fixture builders ---------------------------------------------------------------


def _text_pdf(path: Path) -> Path:
    import reportlab.pdfgen.canvas as canvas

    c = canvas.Canvas(str(path))
    c.setFont("Helvetica", 11)
    c.drawString(72, 720, TEXT)
    c.showPage()
    c.save()
    return path


def _truthful_differences() -> list:
    """codes 32..126 named uniXXXX of their ASCII char — the honest version
    of what the seen-in-the-wild generators emit."""
    return [32] + [f"/uni{code:04X}" for code in range(32, 127)]


def _lying_tounicode() -> bytes:
    """Every code claims to be 'A' — the damaging present-but-wrong map."""
    entries = b"\n".join(f"<{code:02x}> <0041>".encode()
                         for code in range(0x20, 0x7F))
    return (b"/CIDInit /ProcSet findresource begin\n12 dict begin\n"
            b"begincmap\n"
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) "
            b"/Supplement 0 >> def\n"
            b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
            b"95 beginbfchar\n" + entries + b"\nendbfchar\n"
            b"endcmap\nCMapName currentdict /CMap defineresource pop\n"
            b"end\nend\n")


def _surgery(pdf: Path, *, differences: list | None, tounicode: bytes | None,
             drop_tounicode: bool = False) -> None:
    import pikepdf

    with pikepdf.open(pdf, allow_overwriting_input=True) as doc:
        for obj in doc.objects:
            try:
                if obj.get("/Type") != pikepdf.Name("/Font"):
                    continue
            except (AttributeError, TypeError):
                continue
            if differences is not None:
                obj.Encoding = pikepdf.Dictionary(
                    Differences=pikepdf.Array(differences))
            if tounicode is not None:
                obj.ToUnicode = pikepdf.Stream(doc, tounicode)
            if drop_tounicode and "/ToUnicode" in obj:
                del obj.ToUnicode
        doc.save(pdf)


def _scanned_pdf(path: Path) -> Path:
    """One page, one full-page image, zero text."""
    import io

    import PIL.Image
    import reportlab.lib.utils as rlutils
    import reportlab.pdfgen.canvas as canvas

    image = PIL.Image.new("RGB", (60, 80), "lightgrey")
    c = canvas.Canvas(str(path))
    c.drawImage(rlutils.ImageReader(io.BytesIO(
        _png_bytes(image))), 0, 0, 600, 800)
    c.showPage()
    c.save()
    return path


def _png_bytes(image) -> bytes:
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _all_text(frames: dict) -> str:
    return " ".join(item[0] for line in frames["statements"][0]["lines"]
                    for item in line["items"])


# --- healing ----------------------------------------------------------------------


def test_sane_pdf_needs_no_healing(tmp_path):
    pdf = _text_pdf(tmp_path / "sane.pdf")
    assert healable_fonts(pdf) == []
    frames = frames_for([pdf], tmp_path)
    assert TEXT in _all_text(frames)
    assert "healed" not in frames  # original bytes, no healed copy
    assert not (tmp_path / "healed").exists()


def test_wrong_tounicode_is_healed_into_true_text(tmp_path):
    pdf = _text_pdf(tmp_path / "lying.pdf")
    _surgery(pdf, differences=_truthful_differences(),
             tounicode=_lying_tounicode())

    import pdfplumber

    with pdfplumber.open(str(pdf)) as doc:  # the default extractor is fooled
        garbled = doc.pages[0].extract_text()
        assert set(garbled) <= {"A", " "} and "A" in garbled

    findings = healable_fonts(pdf)
    assert len(findings) == 1 and findings[0]["codes"] == 95  # codes 32..126
    assert "disagrees" in findings[0]["reason"]

    frames = frames_for([pdf], tmp_path)
    assert frames["healed"] == [pdf.name]  # provenance rides on the frames
    assert frames["statements"][0]["name"] == pdf.name  # name is preserved
    assert TEXT in _all_text(frames)  # extraction reads the true text now
    healed = tmp_path / "healed" / pdf.name
    assert healed.exists()
    assert healable_fonts(healed) == []  # healing is idempotent


def test_missing_tounicode_is_healable(tmp_path):
    pdf = _text_pdf(tmp_path / "notounicode.pdf")
    _surgery(pdf, differences=_truthful_differences(), tounicode=None,
             drop_tounicode=True)
    findings = healable_fonts(pdf)
    assert len(findings) == 1
    assert findings[0]["reason"] == "no ToUnicode"
    frames = frames_for([pdf], tmp_path)
    assert TEXT in _all_text(frames)  # correct whether or not the extractor
    # already resolves uniXXXX names itself


def test_heal_failure_degrades_to_original(tmp_path, monkeypatch):
    pdf = _text_pdf(tmp_path / "lying.pdf")
    _surgery(pdf, differences=_truthful_differences(),
             tounicode=_lying_tounicode())

    def boom(pdf_in, pdf_out):
        raise RuntimeError("pikepdf exploded")

    monkeypatch.setattr("statementledger.frames.heal", boom)
    frames = frames_for([pdf], tmp_path)  # no crash, extraction proceeds
    assert frames["statements"][0]["lines"]  # text came out (garbled or not,
    # the run is not blocked by triage)


# --- scanned detection ---------------------------------------------------------------


def test_scanned_pdf_raises(tmp_path):
    pdf = _scanned_pdf(tmp_path / "scan.pdf")
    with pytest.raises(ScannedPdfError, match="scanned"):
        frames_for([pdf], tmp_path)
    assert not (tmp_path / "frames.json").exists()  # nothing half-written


def test_empty_but_not_scanned_pdf_does_not_raise(tmp_path):
    import reportlab.pdfgen.canvas as canvas

    pdf = tmp_path / "blank.pdf"
    c = canvas.Canvas(str(pdf))  # one page, no text, no image
    c.showPage()
    c.save()
    frames = frames_for([pdf], tmp_path)
    assert frames["statements"][0]["lines"] == []
    assert "healed" not in frames


def test_scanned_surfaces_as_note_in_web_probe(tmp_path):
    pdf = _scanned_pdf(tmp_path / "scan.pdf")
    from web import parsing

    result = parsing.probe(pdf, [])
    assert result["ok"] is False
    assert "scanned" in result["note"].lower()
    assert "OCR" in result["note"]
