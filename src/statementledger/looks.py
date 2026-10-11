"""Look events: which frames lines a render/grep/table call examined.

The distill agent emits these directly, and the web layer derives the same
ones from legacy trace.log workdirs — one implementation so the two paths
cannot drift. Pure geometry over frames lines; no PDF is touched.
"""

from __future__ import annotations

import json

SELECT_BAND = 11  # pt: highlight band around a frames line (y is the line's
# baseline; pdfplumber y grows downward from the page top)


def json_object(text: str) -> dict:
    """First line of `text` as a JSON object (tool args are single-line
    dumps; anything else — including multi-line junk — is no args)."""
    try:
        value = json.loads(text.splitlines()[0]) if text.strip() else None
    except (ValueError, IndexError):
        return {}
    return value if isinstance(value, dict) else {}


def look_event(turn: int, tool: str, args: dict, result: str,
               lines: list[dict]) -> dict | None:
    """render/grep/table arguments -> the line span they examined, resolved
    against frames into an overlay rect (pdfplumber points, y downward).
    None when the call did not point at any statement lines."""
    label = ""
    ids: list[int] = []
    if tool == "render":
        start = max(0, int(args.get("start", 0) or 0))
        n = max(1, min(int(args.get("n", 40) or 40), 120))
        ids = list(range(start, min(len(lines), start + n)))
        label = f"render {start}+{len(ids)}"
    elif tool == "grep":
        payload = json_object(result)
        hits = [hit[0] for hit in payload.get("hits", [])
                if isinstance(hit, list) and isinstance(hit[0], int)]
        ids = sorted(set(hits))
        label = f"grep {str(args.get('pattern', ''))[:40]}"
    elif tool == "table":
        start, stop = args.get("start"), args.get("stop")
        if isinstance(start, int) and isinstance(stop, int):
            ids = list(range(start, min(len(lines), stop)))
            label = f"table {start}..{stop}"
    ids = [i for i in ids if 0 <= i < len(lines)]
    if not ids:
        return None
    page = lines[ids[0]]["page"]
    same_page = [i for i in ids if lines[i]["page"] == page]
    if not same_page:
        return None
    items = [item for i in same_page for item in lines[i]["items"]]
    x1 = min((item[1] for item in items), default=0)
    x2 = max((item[2] for item in items), default=0)
    top = min(lines[i]["y"] for i in same_page) - SELECT_BAND
    bottom = max(lines[i]["y"] for i in same_page) + 2
    return {"type": "look", "turn": turn, "tool": tool, "label": label,
            "page": page, "rect": [x1, top, x2, bottom],
            "line_ids": same_page}
