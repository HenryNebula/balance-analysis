"""Dump the structure of a .docx statement template: tables, paragraphs,
header/footer, fonts — to decide how to fill each one.

    uv run python tools/templates/inspect_template.py data/tl_docx/tl-44773.docx
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from docx import Document

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def cell_text(cell) -> str:
    return " / ".join(p.text for p in cell.paragraphs if p.text.strip())


def main() -> None:
    path = Path(sys.argv[1])
    doc = Document(path)
    sec = doc.sections[0]
    print(f"== {path.name} ==")
    print(f"page: {sec.page_width.inches:.2f} x {sec.page_height.inches:.2f} in  "
          f"margins L{sec.left_margin.inches:.2f} R{sec.right_margin.inches:.2f} "
          f"T{sec.top_margin.inches:.2f} B{sec.bottom_margin.inches:.2f}")

    print(f"\n-- header: {[p.text for p in sec.header.paragraphs if p.text.strip()]}")
    print(f"-- footer: {[p.text for p in sec.footer.paragraphs if p.text.strip()]}")

    print(f"\n-- tables: {len(doc.tables)}")
    for ti, t in enumerate(doc.tables):
        grid = set()
        for row in t.rows:
            for c in row.cells:
                grid.add(c._tc.get(
                    f"{{{NS['w']}}}gridSpan", "1"))
        print(f"  table[{ti}] {len(t.rows)} rows x {len(t.columns)} cols "
              f"(style={t.style.name if t.style else None})")
        for ri, row in enumerate(t.rows[:4]):
            texts = [cell_text(c)[:28] for c in row.cells]
            print(f"    r{ri}: {texts}")
        if len(t.rows) > 4:
            print(f"    … ({len(t.rows) - 4} more rows)")
            for ri in range(len(t.rows) - 2, len(t.rows)):
                texts = [cell_text(c)[:28] for c in t.rows[ri].cells]
                print(f"    r{ri}: {texts}")

    # body order: paragraphs and tables interleaved
    print("\n-- body flow (first 25 blocks):")
    body = doc.element.body
    count = 0
    for child in body:
        tag = child.tag.split("}")[1]
        if tag == "p":
            texts = [t.text or "" for t in child.findall(".//w:t", NS)]
            joined = "".join(texts).strip()
            if joined:
                print(f"  p: {joined[:100]}")
                count += 1
        elif tag == "tbl":
            first = "".join(
                t.text or "" for t in child.findall(".//w:t", NS))[:60]
            print(f"  TABLE: {first}…")
            count += 1
        if count >= 25:
            print("  …")
            break

    # fonts declared in styles
    with zipfile.ZipFile(path) as z:
        styles = z.read("word/styles.xml").decode("utf-8", "replace")
    fonts = sorted(set(re.findall(r'w:ascii="([^"]+)"', styles)))
    print(f"\n-- fonts in styles.xml: {fonts}")


if __name__ == "__main__":
    main()
