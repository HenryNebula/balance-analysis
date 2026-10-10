"""Render the fixed visual-review tour for a generated statement corpus.

The tour is the evaluation function of the ui-review-loop: the SAME pages,
in the SAME order, every round, so verdicts stay comparable. It covers the
shortest month (tail-block behavior), the longest (multi-page table,
repeated header rows), and every T&C page.

    uv run python tools/templates/render_tour.py data/gen/tl-44769

Tour definition (fixed): all pages of the shortest and the longest month,
plus the final (T&C) page of every other month. Renders at 100 DPI.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    corpus = Path(sys.argv[1])
    manifest = [json.loads(line) for line in
                (corpus / "manifest.jsonl").read_text().splitlines() if line.strip()]
    by_pages = sorted(manifest, key=lambda r: r["pages"])
    shortest, longest = by_pages[0], by_pages[-1]
    tour_months = {shortest["month"], longest["month"]}

    out = corpus / "tour"
    out.mkdir(parents=True, exist_ok=True)
    shots = []
    for row in manifest:
        pdf = corpus / row["file"]
        if row["month"] in tour_months:
            stem = row["month"]
            subprocess.run(["pdftoppm", "-r", "100", "-png", str(pdf),
                            str(out / stem)], check=True)
            for i in range(1, row["pages"] + 1):
                shots.append(out / f"{stem}-{i}.png")
        else:                                   # final (T&C) page only
            stem = row["month"]
            subprocess.run(["pdftoppm", "-r", "100", "-png", "-f",
                            str(row["pages"]), "-l", str(row["pages"]),
                            str(pdf), str(out / stem)], check=True)
            shots.append(out / f"{stem}-{row['pages']}.png")
    print(f"tour: {len(shots)} pages "
          f"(full months {shortest['month']}+{longest['month']}, T&C pages rest)")
    for s in shots:
        print(" ", s.relative_to(corpus))


if __name__ == "__main__":
    main()
