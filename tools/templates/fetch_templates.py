"""Fetch the TemplateLab .docx statement-template originals into data/tl_docx/.

The converted PDFs in the main checkout's data/templatelab came from
Word/Excel originals pulled from Wayback Machine captures of
templatelab.com's ?wpdmdl=<id> download endpoints (the live site is
Cloudflare-gated). The originals were deleted after conversion; this
re-fetches the .docx ones we want as fill targets, pinning sha256s in
data/tl_docx/metadata.jsonl.

© TemplateLab: templates and everything derived from them stay in
gitignored data/ — never committed, never re-hosted.

    uv run python tools/templates/fetch_templates.py [id ...]
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[1]
META = WORKTREE / "data" / "templatelab" / "metadata.jsonl"
OUT = WORKTREE / "data" / "tl_docx"

# Shortlist: realistic multi-page-capable statement layouts with a
# transaction table (notes from the corpus metadata).
DEFAULT_WANT = {"tl-44745", "tl-44773", "tl-44747", "tl-44767",
                "tl-44769", "tl-44771"}


def main() -> None:
    want = set(sys.argv[1:]) or DEFAULT_WANT
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for line in open(META):
        r = json.loads(line)
        if r["id"] not in want:
            continue
        url = r["origin_url"]
        dest = OUT / f"{r['id']}.docx"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())
        sha = hashlib.sha256(dest.read_bytes()).hexdigest()
        print(f"{dest.name}: {dest.stat().st_size:,} bytes  sha256={sha[:16]}…")
        rows.append({"id": r["id"], "file": dest.name, "origin_url": url,
                     "notes": r["notes"], "sha256": sha})
    with (OUT / "metadata.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"wrote {OUT / 'metadata.jsonl'} ({len(rows)} templates)")


if __name__ == "__main__":
    main()
