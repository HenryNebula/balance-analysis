"""Frames snapshots: the committed extraction of each fixture PDF. A
pdfplumber (or frames.py) change that alters geometry, grouping, or item
text shows up here first — these files ARE the extractor's contract with
the registry parsers."""

import json
from pathlib import Path

import statementledger as sl

FIXTURES = Path(__file__).parent / "fixtures"


def test_fixture_snapshots_hold():
    import tempfile

    for snapshot in sorted(FIXTURES.glob("*.frames.json")):
        pdf = FIXTURES / (snapshot.name.replace(".frames.json", ".pdf"))
        with tempfile.TemporaryDirectory() as tmp:
            frames = sl.frames_for([pdf], Path(tmp))
        committed = json.loads(snapshot.read_text())
        assert frames == committed, (
            f"{snapshot.name} drifted — regenerate with "
            "tools/fixtures/gen_fixtures.py and review the diff (a geometry "
            "change here can break every registered parser)")
