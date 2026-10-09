"""Server-side PDF rendering for overlays (docs/web-ui-design.md §9.3).

Pages raster lazily with pypdfium2 (single self-contained wheel, no system
deps, keeps the coordinate math in Python next to the frames) at scale 2.0
(~144 dpi), cached content-addressed under data/cache/pages/. Frames are
extracted once and cached the same way — the run view's overlay and the
statement-detail provenance overlay share both.

Coordinate contract: frames lines carry {page, y, items: [[text, x1, x2]]}
in pdfplumber points, y measured DOWNWARD from the page top; pdfium's
raster origin is also top-left at natural scale = points. So overlay
pixels = points * SCALE, and the browser scales the whole stack for zoom.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import statementledger as sl

SCALE = 2.0


def _sha16(pdf: Path) -> str:
    return hashlib.sha256(pdf.read_bytes()).hexdigest()[:16]


def page_png(pdf: Path, page_index: int, cache_root: Path) -> Path | None:
    """Render one page to PNG (cached); None when the page does not exist."""
    cache = cache_root / "pages" / _sha16(pdf)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"page_{page_index}.png"
    if target.exists():
        return target
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(pdf))
    try:
        if page_index >= len(document):
            return None
        bitmap = document[page_index].render(scale=SCALE)
        bitmap.to_pil().save(target)
    finally:
        document.close()
    return target


def page_count(pdf: Path, cache_root: Path) -> int:
    """Rendered-page count (from the cached frames — no PDF open needed)."""
    return 1 + max((line["page"] for line in cached_frames(pdf, cache_root)
                    ["lines"]), default=-1)


def cached_frames(pdf: Path, cache_root: Path) -> dict:
    """The one-statement frames dict {name, lines} for a stored PDF,
    extracted once and cached content-addressed."""
    cache = cache_root / "frames"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{_sha16(pdf)}.json"
    if not target.exists():
        with tempfile.TemporaryDirectory() as tmp:
            frames = sl.frames_for([pdf], Path(tmp))
        target.write_text(json.dumps(frames["statements"][0]))
    return json.loads(target.read_text())


def highlight_px(rect: list[float]) -> list[float]:
    """A probe_select rect in pdfplumber points -> CSS pixels at SCALE."""
    return [value * SCALE for value in rect]
