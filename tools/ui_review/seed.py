"""Seed the review-tour data through the app's real code paths (run from
the repo root): three accounts with parsed/failed statements plus two
distill runs — one converged with auto-parse (canned registry parser), one
FAILED attached to a synthetic agent workdir whose trace.log carries the
full trajectory vocabulary (tool chips, looks that paint the PDF overlay,
write/edit snapshots, gates, loop abort). Writes data/review-manifest.json
for the tour script. run_tour.sh wipes state BEFORE the server starts;
this script never deletes anything (it once ate a live account mid-demo).

    uv run --group web python tools/ui_review/seed.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from fastapi.testclient import TestClient  # noqa: E402

from web.app import create_app  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures"
BLUE = FIXTURES / "bluelantern_savings.pdf"
COPPER = FIXTURES / "copperkettle_checking.pdf"
CANNED_FULL = (REPO / "src" / "statementledger" / "registry"
               / "us__bluelantern__savings__1.py")
COPPER_KEY = "us__copperkettle__checking__1"
JSON = {"accept": "application/json"}
TERMINAL = ("converged", "failed", "cancelled", "orphaned")


def upload(client, slug, pdf):
    with open(pdf, "rb") as handle:
        return client.post(f"/accounts/{slug}/statements",
                           files={"file": (pdf.name, handle,
                                           "application/pdf")},
                           headers=JSON).json()


def create(client, name, key=""):
    response = client.post("/accounts", data={"display_name": name,
                                              "registry_key": key},
                           headers=JSON)
    assert response.status_code in (200, 201, 303), response.text


def wait_terminal(client, run_id, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/runs/{run_id}", headers=JSON).json()
        if run["status"] in TERMINAL:
            return run
        time.sleep(0.3)
    raise SystemExit(f"run {run_id} did not terminate")


RULE = "=" * 78
DASH = "-" * 78


def _section(name: str, body: str = "") -> str:
    return f"\n{RULE}\n[10:00:00] {name}\n{DASH}\n{body}\n"


def synthetic_failed_trace() -> str:
    """A believable agent arc that ends FAILED: looks at the header and the
    table, drafts a parser, gates it, chases one reconcile error, then dies
    in a duplicate-call loop. Line indices match the bluelantern fixture."""
    code_v1 = ('KEY = "us__harbour_import__checking__1"\n'
               'def parse(frames):\n    return {"transactions": []}\n')
    old = '{"transactions": []}'
    new = '{"transactions": [], "opening_balance": 0.0}'
    code_v2 = code_v1.replace(old, new)
    return "".join([
        _section("run start",
                 "mode=demo-model at http://demo/v1\npdfs=['blue.pdf']"),
        _section("frames", "1 statement(s); blue.pdf: 24 lines"),
        _section("tool call: grep", '{"pattern": "Account|Balance"}'),
        _section("tool result: grep", json.dumps({
            "hits": [[0, "Blue Lantern Savings Bank"], [2, "Account Number"],
                     [6, "Balance"]], "count": 3})),
        "turn 1: look · 4k chars\n",
        _section("tool call: render", '{"start": 0, "n": 12}'),
        _section("tool result: render", "[Page 0]\n000 | Blue Lantern..."),
        _section("tool call: table",
                 '{"header_index": 8, "stop_terms": ["Page "]}'),
        _section("tool result: table",
                 json.dumps({"columns": ["Date", "Description", "Amount"],
                             "row_count": 14, "rows_shown": 14, "rows": []})),
        "turn 2: look · 11k chars\n",
        _section("tool call: write", json.dumps({"content": code_v1})),
        _section("write parser.py", code_v1),
        _section("tool result: write", "wrote parser.py (3 lines)\nlint: OK"),
        _section("tool call: run", "{}"),
        _section("tool result: run", "0/1 statements reconcile"),
        _section("parser.py statementledger gate", "exit 0\n" + json.dumps({
            "key": "us__harbour_import__checking__1", "stage": "parse",
            "clean": 0, "verified": False, "results": [{
                "name": "blue.pdf", "ok": False, "rows": 0,
                "error": "reconcile broke: opening 0.00 + txns 0.00 "
                         "!= closing 2714.83"}]})),
        "turn 3: 0/1 clean · 14k chars\n",
        "turn 3: duplicate run call (first at turn 3)\n",
        _section("tool call: edit",
                 json.dumps({"old": old, "new": new})),
        _section("edit parser.py", f"- {old!r}\n+ {new!r}"),
        _section("tool result: edit", "replaced 1 occurrence\nlint: OK"),
        _section("tool call: run", "{}"),
        _section("tool result: run", "0/1 statements reconcile"),
        _section("parser.py statementledger gate", "exit 0\n" + json.dumps({
            "key": "us__harbour_import__checking__1", "stage": "parse",
            "clean": 0, "verified": False, "results": [{
                "name": "blue.pdf", "ok": False, "rows": 0,
                "error": "reconcile broke: opening 0.00 + txns 0.00 "
                         "!= closing 2714.83"}]})),
        "turn 4: 0/1 clean · 17k chars\n",
        "turn 4: REFUSED duplicate python call (x3)\n",
        "turn 4: loop abort — 3 consecutive turns of refused duplicate calls\n",
        _section("result", "FAILED (not verified)"),
    ])


DEMO_SLUGS = ("blue-lantern-savings", "copper-kettle-checking",
              "harbour-import-attempts", "run-lab", "probe-playground")


def main() -> None:
    data_root = Path(os.environ.get("STMTLEDGER_WEB_DATA",
                                    str(REPO / "data")))

    client = TestClient(create_app(data_root=data_root,
                                   harness_script=CANNED_FULL))
    for slug in DEMO_SLUGS:  # idempotent: only ever our own demo accounts
        client.delete(f"/accounts/{slug}")

    create(client, "Blue Lantern Savings")
    blue = upload(client, "blue-lantern-savings", BLUE)
    assert blue["status"] == "parsed", blue

    create(client, "Copper Kettle Checking", key=COPPER_KEY)
    copper = upload(client, "copper-kettle-checking", COPPER)
    assert copper["status"] == "parsed", copper

    # pinned to the WRONG parser: a durable failure state (§6.3c)
    create(client, "Harbour Import Attempts", key=COPPER_KEY)
    failed = upload(client, "harbour-import-attempts", BLUE)
    assert failed["status"] == "failed", failed

    # a sacrificial parsed statement the converged run can auto-parse
    create(client, "Run Lab")
    lab = upload(client, "run-lab", BLUE)
    assert lab["status"] == "parsed", lab

    converged = client.post("/runs", json={
        "account_slug": "run-lab", "statement_ids": [lab["id"]],
        "mode": "draft"}, headers=JSON).json()
    run_converged = wait_terminal(client, converged["run_id"])
    assert run_converged["status"] == "converged", run_converged

    # the FAILED run: attach to a synthetic agent workdir — a complete
    # trajectory (tools, looks, snapshots, gates, loop abort) with no LLM
    workdir = data_root / "review-agent-workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "trace.log").write_text(synthetic_failed_trace())
    (workdir / "parser.py").write_text(
        'KEY = "us__harbour_import__checking__1"\n'
        'def parse(frames):\n    return {"transactions": []}\n')
    failed_run = client.post("/runs", json={
        "account_slug": "harbour-import-attempts",
        "statement_ids": [failed["id"]], "mode": "draft",
        "workdir": str(workdir)}, headers=JSON).json()
    run_failed = wait_terminal(client, failed_run["run_id"])
    assert run_failed["status"] == "failed", run_failed

    manifest = {
        "accounts": {
            "parsed": "blue-lantern-savings",
            "pinned": "copper-kettle-checking",
            "failed": "harbour-import-attempts",
            "sacrificial": "probe-playground",
        },
        "failedSid": failed["id"],
        "parsedSid": blue["id"],
        "runConverged": run_converged["id"],
        "runFailed": run_failed["id"],
    }
    (data_root / "review-manifest.json").write_text(
        json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
