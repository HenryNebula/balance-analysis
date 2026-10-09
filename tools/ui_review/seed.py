"""Seed the review-tour data through the app's real code paths (run from
the repo root): three accounts with parsed/failed statements plus two
distill runs (one converged with auto-parse, one failed with live probe
selections for the overlay). Writes data/review-manifest.json for the tour
script. run_tour.sh wipes state BEFORE the server starts; this script
never deletes anything (it once ate a live account mid-demo).

    uv run --group web python tools/ui_review/seed.py
"""

from __future__ import annotations

import json
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
    assert response.status_code in (200, 303), response.text


def wait_terminal(client, run_id, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/runs/{run_id}", headers=JSON).json()
        if run["status"] in TERMINAL:
            return run
        time.sleep(0.3)
    raise SystemExit(f"run {run_id} did not terminate")


def main() -> None:
    data_root = REPO / "data"

    # A canned PROBE script for the failed run (its selections paint the
    # overlay); the registry module itself converges the other run.
    probe_script = data_root / "review_canned_probe.py"
    probe_script.parent.mkdir(exist_ok=True)
    probe_script.write_text(
        '# probe\nimport statementledger as pl\n'
        'lines = pl.load_frames()[0]["lines"]\n'
        'pl.show(lines, 10)\n'
        'print(\'SELECT:{"page": 0, "lines": [0, 10], '
        '"label": "statement header + table top"}\')\n')

    client = TestClient(create_app(data_root=data_root,
                                   harness_script=CANNED_FULL))

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
        "mode": "draft", "rounds": 1}, headers=JSON).json()
    run_converged = wait_terminal(client, converged["run_id"])
    assert run_converged["status"] == "converged", run_converged

    probe_client = TestClient(create_app(data_root=data_root,
                                         harness_script=probe_script))
    failed_run = probe_client.post("/runs", json={
        "account_slug": "harbour-import-attempts",
        "statement_ids": [failed["id"]], "mode": "draft",
        "rounds": 2}, headers=JSON).json()
    run_failed = wait_terminal(probe_client, failed_run["run_id"])
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
