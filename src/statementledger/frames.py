"""PDF → layout frames: the only place statementledger touches a PDF.

frames_for() is a faithful Python port of transtractor's
`pdf_to_text_items` + `text_items_to_layout`
(src/parsers/flows/pdf_to_text_items.rs, MIT — see NOTICE): word extraction
with pdfplumber's x/y tolerances, a dynamic y-binning system that tolerates
small variations in line alignment, merging of adjacent items on the same
line, and the page/y/x sort. transtractor's own word floor (pdfsink-rs) is a
pdfplumber clone, so the semantics carry over one-to-one; the frames this
produces are drop-in identical with the layout dumps the engine was
distilled against (verified item-for-item on the reference fixtures:
identical text, x coordinates, pages and line groupings; line y values can
differ by 1pt — the rust engine rounded f32 boxes, pdfplumber rounds f64 —
which no parser can observe, since y is only ever compared within one
extractor's frames).

Output — the FRAMES contract every registered parser is written against:

    {"statements": [{"name": "test1.pdf",
                     "lines": [{"page": 0, "y": 700,
                                "items": [["Account number:", 20, 150],
                                          ["1234 5678", 160, 320]]},
                               ...]},
                    ...]}

ONE element per statement; parse(frames) receives ONE of those elements.
y grows downward (it is the binned bottom edge of the line's first item);
items are x-ordered [text, x1, x2] triples.
"""

from __future__ import annotations

import json
from pathlib import Path

# Same constants the rust engine used: 2pt/3pt word tolerances (pdfplumber's
# extract_words parameters), 3pt line-alignment tolerance, one-average-char
# gap for same-line item merging.
X_TOL = 2
Y_TOL = 3
X_GAP_CHARS = 1


def load_frames() -> list[dict]:
    """The working directory's statements, from frames.json —
    [{"name": "test1.pdf", "lines": [...]}, ...]
    ONE element per statement. parse(frames) receives ONE of those elements
    (a single statement dict) — not this list."""
    data = json.loads(Path("frames.json").read_text())
    return data["statements"]


def _round(value: float) -> int:
    """f32.round() as i32 with a floor at zero, as the rust engine had:
    round half away from zero, clamped to >= 0."""
    return max(0, int(value + 0.5)) if value >= 0 else 0


class _Bins:
    """Occupied y-coordinate bins (the rust engine's dynamic line registry):
    remembering which y values already anchor a line lets an out-of-order
    item (a superscript, a wrapped row printed slightly high) snap to its
    true line instead of seeding a phantom one."""

    def __init__(self) -> None:
        self._bins = [False] * 1001

    def closest(self, y1: int, tolerance: int) -> int | None:
        if 0 <= y1 < len(self._bins) and self._bins[y1]:
            return y1
        for offset in range(-tolerance, tolerance + 1):
            check = y1 + offset
            if 0 <= check < len(self._bins) and self._bins[check]:
                return check
        return None

    def set(self, y1: int) -> None:
        if y1 >= len(self._bins):
            self._bins.extend([False] * (y1 + 500 - len(self._bins)))
        self._bins[y1] = True


def _avg_char_width(item: dict) -> float:
    width = item["x2"] - item["x1"]
    # bytes, not characters — matches the rust engine (text.len() is UTF-8)
    num_chars = len(item["text"].encode("utf-8"))
    return width / num_chars if num_chars else 0.0


def _text_items(pdf: Path) -> list[dict]:
    """Extract one PDF into canonically ordered text items:
    [{text, x1, y1, x2, y2, page, y1_bin}] sorted by (page, y1_bin, x1)."""
    import pdfplumber

    text_items: list[dict] = []
    y1_bins = _Bins()

    with pdfplumber.open(str(pdf)) as doc:
        for page_index, page in enumerate(doc.pages):
            curr_y1_bin = 0
            for word in page.extract_words(x_tolerance=X_TOL, y_tolerance=Y_TOL):
                # Non-upright / vertical text never carries statement data
                # and interferes with extraction — the rust engine skipped it.
                if not word.get("upright", True):
                    continue
                text = word["text"].strip()
                if not text:
                    continue
                item = {
                    "text": text,
                    "x1": _round(word["x0"]),
                    "y1": _round(word["bottom"]),
                    "x2": _round(word["x1"]),
                    "y2": _round(word["top"]),
                    "page": page_index,
                    "y1_bin": _round(word["bottom"]),
                }

                # Assign the item to a y1_bin for line grouping. Same line
                # as the previous item...
                if abs(item["y1"] - curr_y1_bin) <= Y_TOL:
                    item["y1_bin"] = curr_y1_bin
                # ...a NEW line below everything seen so far on this page...
                elif item["y1"] > curr_y1_bin + Y_TOL:
                    y1_bins.set(item["y1"])
                    curr_y1_bin = item["y1"]
                # ...or an out-of-order item: snap to the closest existing
                # line (extended tolerance), else seed a new one.
                else:
                    closest = y1_bins.closest(item["y1"], Y_TOL * 2)
                    if closest is not None:
                        item["y1_bin"] = closest
                    else:
                        y1_bins.set(item["y1"])
                        curr_y1_bin = item["y1"]

                # Merge with the previous item when both sit on the same
                # line and the horizontal gap is under one average character
                # width (a word the extractor split, e.g. across a kern).
                if text_items:
                    last = text_items[-1]
                    char_width = (_avg_char_width(last) + _avg_char_width(item)) / 2
                    max_distance = X_GAP_CHARS * int(char_width)
                    top_diff = abs(item["y1"] - last["y1"])
                    bottom_diff = abs(item["y2"] - last["y2"])
                    horizontal_gap = item["x1"] - last["x2"]
                    if (top_diff <= 3 and bottom_diff <= 3
                            and 0 <= horizontal_gap <= max_distance):
                        last["text"] = f"{last['text']} {item['text']}"
                        last["x1"] = min(last["x1"], item["x1"])
                        last["x2"] = max(last["x2"], item["x2"])
                        last["y1"] = max(last["y1"], item["y1"])
                        last["y2"] = min(last["y2"], item["y2"])
                        last["y1_bin"] = min(last["y1_bin"], item["y1_bin"])
                        continue

                text_items.append(item)

    text_items.sort(key=lambda it: (it["page"], it["y1_bin"], it["x1"]))
    return text_items


def _statement(pdf: Path) -> dict:
    """One PDF -> one statement dict: same (page, y1_bin) runs are a line,
    in page/y/x order. A line's "y" is its FIRST (leftmost) item's y1 — the
    bin only decides grouping, the stored y stays real geometry."""
    lines = []
    curr_bin = None
    for item in _text_items(pdf):
        if lines and item["page"] == lines[-1]["page"] and item["y1_bin"] == curr_bin:
            lines[-1]["items"].append([item["text"], item["x1"], item["x2"]])
        else:
            curr_bin = item["y1_bin"]
            lines.append({"page": item["page"], "y": item["y1"],
                          "items": [[item["text"], item["x1"], item["x2"]]]})
    return {"name": pdf.name, "lines": lines}


def frames_for(pdfs: list[Path], workdir: Path) -> dict:
    """Extract layout frames for the pdfs and write frames.json into workdir
    (the distill sandbox and registered parsers read it from there)."""
    frames = {"statements": [_statement(pdf) for pdf in pdfs]}
    (workdir / "frames.json").write_text(json.dumps(frames))
    return frames
