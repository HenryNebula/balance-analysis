"""Web UI Phase 1 (docs/web-ui-design.md, design-history appendix): accounts, upload + sync parse
(pinned and detection), statement detail, failures surfaced with structured
per-parser attempts, registry pages. Runs against a temp data root."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web.app import create_app
from web.store import slugify, statement_id

FIXTURES = Path(__file__).parent / "fixtures"
BLUE = FIXTURES / "bluelantern_savings.pdf"
BLUE_KEY = "us__bluelantern__savings__1"
COPPER = FIXTURES / "copperkettle_checking.pdf"
COPPER_KEY = "us__copperkettle__checking__1"
JSON = {"accept": "application/json"}


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(data_root=tmp_path / "data"))


def upload(client, slug, pdf, headers=None, name=None):
    with open(pdf, "rb") as handle:
        return client.post(
            f"/accounts/{slug}/statements",
            files={"file": (name or Path(pdf).name, handle, "application/pdf")},
            headers=headers or JSON)


def create(client, name, key="", headers=None):
    return client.post("/accounts", data={"display_name": name,
                                          "registry_key": key},
                       headers=headers or JSON)


def test_slugify_and_id():
    assert slugify("Blue Lantern Savings!") == "blue-lantern-savings"
    assert slugify("  --A--B--  ") == "a-b"
    assert len(statement_id(b"x")) == 16


def test_index_empty(client):
    assert client.get("/", headers=JSON).json() == {"accounts": []}


def test_create_account(client, tmp_path):
    response = create(client, "Blue Lantern Savings")
    assert response.status_code in (200, 201, 303)
    account_file = (tmp_path / "data" / "accounts" / "blue-lantern-savings"
                    / "account.json")
    account = json.loads(account_file.read_text())
    assert account["registry_key"] is None
    assert account["slug"] == "blue-lantern-savings"
    rows = client.get("/", headers=JSON).json()["accounts"]
    assert any(r["account"]["slug"] == "blue-lantern-savings" for r in rows)


def test_create_account_duplicate_slug(client):
    assert create(client, "Blue Lantern Savings").status_code in (200, 201, 303)
    assert create(client, "Blue Lantern Savings").status_code == 409


def test_create_account_validation(client):
    assert create(client, "   ").status_code == 400
    assert create(client, "!!!") .status_code == 400  # no slug-able characters
    assert create(client, "X", key="not__a__key__1").status_code == 400


def test_upload_detection_parsed(client, tmp_path):
    create(client, "Blue Lantern Savings")
    response = upload(client, "blue-lantern-savings", BLUE)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "parsed"
    assert body["key"] == BLUE_KEY
    assert body["suggest_pin"] == BLUE_KEY  # detection: exactly one verified
    truth = json.loads((FIXTURES / "bluelantern_savings.truth.json").read_text())
    assert body["summary"]["transactions"] == len(truth["transactions"])
    assert body["summary"]["closing_balance"] == truth["closing_balance"]

    meta_dir = (tmp_path / "data" / "accounts" / "blue-lantern-savings"
                / "statements" / body["id"])
    assert (meta_dir / "original.pdf").read_bytes() == BLUE.read_bytes()
    assert (meta_dir / "statement.json").exists()  # written only after verify
    meta = json.loads((meta_dir / "meta.json").read_text())
    assert meta["status"] == "parsed"
    dates = [t["date"] for t in truth["transactions"] if t["date"]]
    assert meta["period"] == {"start": min(dates), "end": max(dates)}
    assert meta["history"][-1]["key"] == BLUE_KEY


def test_upload_duplicate(client):
    create(client, "Blue Lantern Savings")
    assert upload(client, "blue-lantern-savings", BLUE).status_code == 201
    again = upload(client, "blue-lantern-savings", BLUE)
    assert again.status_code == 409
    assert again.json()["status"] == "duplicate"


def test_upload_pinned_wrong_key_fails_with_attempts(client, tmp_path):
    create(client, "Blue Lantern Savings", key=COPPER_KEY)
    response = upload(client, "blue-lantern-savings", BLUE)
    assert response.status_code == 200  # domain outcome, not an error (§2)
    body = response.json()
    assert body["status"] == "failed"
    assert [a["key"] for a in body["attempts"]] == [COPPER_KEY]
    assert body["attempts"][0]["error"]
    statement_dir = (tmp_path / "data" / "accounts" / "blue-lantern-savings"
                     / "statements" / body["id"])
    assert (statement_dir / "original.pdf").exists()  # upload stayed durable
    assert not (statement_dir / "statement.json").exists()


def test_upload_not_a_pdf(client):
    create(client, "Blue Lantern Savings")
    response = client.post(
        "/accounts/blue-lantern-savings/statements",
        files={"file": ("stmt.pdf", b"hello, definitely not a pdf",
                        "application/pdf")}, headers=JSON)
    assert response.status_code == 400


def test_upload_too_large(tmp_path):
    client = TestClient(create_app(data_root=tmp_path / "data", max_mb=0.001))
    create(client, "Blue Lantern Savings")
    assert upload(client, "blue-lantern-savings", BLUE).status_code == 413


def test_upload_scanned_pdf_gets_plain_note(client):
    from io import BytesIO

    import PIL.Image
    import reportlab.lib.utils as rlutils
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    png = BytesIO()
    PIL.Image.new("RGB", (60, 80), "lightgrey").save(png, format="PNG")
    page = canvas.Canvas(buffer)  # one page, one full-page image, zero text
    page.drawImage(rlutils.ImageReader(png), 0, 0, 600, 800)
    page.showPage()
    page.save()
    create(client, "Blue Lantern Savings")
    response = client.post(
        "/accounts/blue-lantern-savings/statements",
        files={"file": ("scan.pdf", buffer.getvalue(), "application/pdf")},
        headers=JSON)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["attempts"] == []
    assert "scanned" in body["note"].lower()
    assert "OCR" in body["note"]  # the actionable next step


def test_upload_encrypted_pdf_gets_plain_note(client, tmp_path):
    import pikepdf

    encrypted = tmp_path / "encrypted.pdf"
    with pikepdf.open(BLUE) as pdf:
        pdf.save(encrypted, encryption=pikepdf.Encryption(owner="o", user="pw"))
    create(client, "Blue Lantern Savings")
    response = upload(client, "blue-lantern-savings", encrypted)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert "encrypted" in body["note"].lower()


def test_statement_detail_and_original(client):
    create(client, "Blue Lantern Savings")
    sid = upload(client, "blue-lantern-savings", BLUE).json()["id"]
    detail = client.get(f"/accounts/blue-lantern-savings/statements/{sid}",
                        headers=JSON).json()
    assert detail["verified"] is True
    truth = json.loads((FIXTURES / "bluelantern_savings.truth.json").read_text())
    descriptions = [t["description"] for t in detail["ledger"]["transactions"]]
    assert truth["transactions"][0]["description"] in descriptions
    original = client.get(
        f"/accounts/blue-lantern-savings/statements/{sid}/original.pdf")
    assert original.status_code == 200
    assert original.content == BLUE.read_bytes()
    assert original.headers["content-type"].startswith("application/pdf")


def test_statement_404s(client):
    create(client, "Blue Lantern Savings")
    missing = client.get("/accounts/blue-lantern-savings/statements/0000000000000000",
                         headers=JSON)
    assert missing.status_code == 404
    assert client.get("/accounts/nope", headers=JSON).status_code == 404


def test_reparse_after_repin(client):
    create(client, "Blue Lantern Savings", key=COPPER_KEY)
    sid = upload(client, "blue-lantern-savings", BLUE).json()["id"]
    failed = client.get(
        f"/accounts/blue-lantern-savings/statements/{sid}",
        headers=JSON).json()
    assert failed["meta"]["status"] == "failed"
    assert failed["ledger"] is None
    # pin the right parser, then re-parse the stored PDF
    pinned = client.post("/accounts/blue-lantern-savings/key",
                         data={"registry_key": BLUE_KEY}, headers=JSON)
    assert pinned.status_code in (200, 303)
    reparsed = client.post(
        f"/accounts/blue-lantern-savings/statements/{sid}/parse", headers=JSON)
    assert reparsed.status_code == 200
    assert reparsed.json()["status"] == "parsed"
    assert reparsed.json()["key"] == BLUE_KEY


def test_key_override_on_reparse(client):
    create(client, "Blue Lantern Savings", key=COPPER_KEY)
    sid = upload(client, "blue-lantern-savings", BLUE).json()["id"]
    bad = client.post(f"/accounts/blue-lantern-savings/statements/{sid}/parse",
                      data={"key": "us__nosuch__bank__1"}, headers=JSON)
    assert bad.status_code == 400
    good = client.post(f"/accounts/blue-lantern-savings/statements/{sid}/parse",
                       data={"key": BLUE_KEY}, headers=JSON)
    assert good.json()["status"] == "parsed"


def test_unpin(client):
    create(client, "Blue Lantern Savings", key=BLUE_KEY)
    response = client.post("/accounts/blue-lantern-savings/key",
                           data={"registry_key": ""}, headers=JSON)
    assert response.status_code == 200
    assert client.get("/accounts/blue-lantern-savings",
                      headers=JSON).json()["account"]["registry_key"] is None


def test_delete_statement(client):
    create(client, "Blue Lantern Savings")
    sid = upload(client, "blue-lantern-savings", BLUE).json()["id"]
    assert client.delete(
        f"/accounts/blue-lantern-savings/statements/{sid}").status_code == 204
    assert client.get(f"/accounts/blue-lantern-savings/statements/{sid}",
                      headers=JSON).status_code == 404


def test_delete_account(client, tmp_path):
    create(client, "Blue Lantern Savings")
    upload(client, "blue-lantern-savings", BLUE)
    assert client.delete("/accounts/blue-lantern-savings").status_code == 204
    assert client.get("/accounts/blue-lantern-savings",
                      headers=JSON).status_code == 404
    assert not (tmp_path / "data" / "accounts" / "blue-lantern-savings").exists()


def test_registry(client, tmp_path):
    create(client, "Blue Lantern Savings")
    upload(client, "blue-lantern-savings", BLUE)
    data = client.get("/registry.json").json()
    keys = {row["key"]: row for row in data}
    assert BLUE_KEY in keys and keys[BLUE_KEY]["description"]
    assert keys[BLUE_KEY]["statements"] == 1  # usage counts for the SPA
    import statementledger as sl
    assert set(keys) == set(sl.keys())  # every module, docstring or not
    # revision provenance columns (§3.3): a built-in row carries the sha of
    # the file a load would exec and its mtime, with no overlay history
    from statementledger import registry
    row = keys[BLUE_KEY]
    overlay = tmp_path / "data" / "registry"
    assert row["sha16"] == registry.current_sha(BLUE_KEY, overlay)
    assert row["updated_at"]
    assert row["revisions"] == 0


def test_two_accounts_same_pdf_allowed(client):
    # same PDF in a different account is allowed (folders self-contained, §10)
    create(client, "Blue Lantern Savings")
    create(client, "Blue Lantern Again")
    assert upload(client, "blue-lantern-savings", BLUE).status_code == 201
    assert upload(client, "blue-lantern-again", BLUE).status_code == 201


# --- Phase 2: probe, re-parse-all, exports, chain/overlap -------------------


def test_probe_stored_statement_nothing_persisted(client, tmp_path):
    create(client, "Blue Lantern Savings", key=COPPER_KEY)
    sid = upload(client, "blue-lantern-savings", BLUE).json()["id"]
    response = client.post("/accounts/blue-lantern-savings/probe",
                           json={"statement_id": sid}, headers=JSON)
    assert response.status_code == 200
    body = response.json()
    by_key = {row["key"]: row for row in body["results"]}
    assert set(by_key) == set(sl_keys())
    assert by_key[BLUE_KEY]["ok"] and by_key[BLUE_KEY]["rows"] > 0
    assert not by_key[COPPER_KEY]["ok"]
    assert body["verified_count"] == 1
    assert body["suggested_key"] == BLUE_KEY
    # a probe never touches the store
    meta = json.loads(statement_dir(tmp_path, sid, "blue-lantern-savings")
                      .joinpath("meta.json").read_text())
    assert meta["status"] == "failed"
    assert len(meta["history"]) == 1


def sl_keys():
    import statementledger as sl
    return sl.keys()


def statement_dir(tmp_path, sid, slug="blue-lantern-savings"):
    return (tmp_path / "data" / "accounts" / slug / "statements" / sid)


def test_probe_uploaded_file(client, tmp_path):
    create(client, "Probe Account")
    with open(BLUE, "rb") as handle:
        response = client.post(
            "/accounts/probe-account/probe",
            files={"file": ("x.pdf", handle, "application/pdf")},
            headers=JSON)
    assert response.status_code == 200
    assert response.json()["suggested_key"] == BLUE_KEY
    assert not list((tmp_path / "data" / "accounts" / "probe-account"
                     / "statements").glob("*"))  # nothing stored


def test_probe_bad_payloads(client):
    create(client, "Blue Lantern Savings")
    assert client.post("/accounts/blue-lantern-savings/probe",
                       json={}, headers=JSON).status_code == 400
    assert client.post("/accounts/blue-lantern-savings/probe",
                       json={"statement_id": "0" * 16},
                       headers=JSON).status_code == 404
    with open(BLUE, "rb") as handle:
        assert client.post(
            "/accounts/blue-lantern-savings/probe",
            files={"file": ("x.pdf", b"not a pdf", "application/pdf")},
            headers=JSON).status_code == 400


def test_reparse_all_report(client):
    create(client, "Blue Lantern Savings", key=COPPER_KEY)
    upload(client, "blue-lantern-savings", BLUE)  # fails (wrong pin)
    client.post("/accounts/blue-lantern-savings/key",
                data={"registry_key": BLUE_KEY}, headers=JSON)
    response = client.post("/accounts/blue-lantern-savings/reparse",
                           headers=JSON)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    entry = body["results"][0]
    assert entry["old"]["status"] == "failed"
    assert entry["new"]["status"] == "parsed"
    assert entry["changed"]

    again = client.post("/accounts/blue-lantern-savings/reparse",
                        headers=JSON).json()
    assert again["results"][0]["changed"] is False  # stable re-parse


def test_reparse_all_requires_pin(client):
    create(client, "Blue Lantern Savings")
    response = client.post("/accounts/blue-lantern-savings/reparse",
                           headers=JSON)
    assert response.status_code == 400


def test_statement_exports(client):
    create(client, "Blue Lantern Savings")
    sid = upload(client, "blue-lantern-savings", BLUE).json()["id"]
    ledger_response = client.get(
        f"/accounts/blue-lantern-savings/statements/{sid}/ledger.json")
    assert ledger_response.status_code == 200
    exported = ledger_response.json()  # the canonical dict, verbatim
    assert exported["_key"] == BLUE_KEY
    assert exported["transactions"]

    csv_response = client.get(
        f"/accounts/blue-lantern-savings/statements/{sid}/ledger.csv")
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    lines = csv_response.text.strip().splitlines()
    assert lines[0] == "date,description,amount,balance"
    assert len(lines) - 1 == len(exported["transactions"])


def test_export_not_parsed_409(client):
    # pin an account to the wrong parser so the stored statement never parses
    create(client, "Fail Account", key=COPPER_KEY)
    with open(BLUE, "rb") as handle:
        outcome = client.post("/accounts/fail-account/statements",
                              files={"file": ("b.pdf", handle,
                                              "application/pdf")},
                              headers=JSON).json()
    assert outcome["status"] == "failed"
    for ext in ("json", "csv"):
        response = client.get(
            f"/accounts/fail-account/statements/{outcome['id']}/ledger.{ext}")
        assert response.status_code == 409


def test_merged_account_ledger(client):
    create(client, "Blue Lantern Savings")
    sid_blue = upload(client, "blue-lantern-savings", BLUE).json()["id"]
    sid_copper = upload(client, "blue-lantern-savings", COPPER).json()["id"]
    merged = client.get("/accounts/blue-lantern-savings/ledger.json").json()
    assert {s["id"] for s in merged["statements"]} == {sid_blue, sid_copper}
    txns = merged["transactions"]
    assert len(txns) == 14 + copper_txn_count()
    dates = [t["date"] for t in txns if t["date"]]
    assert dates == sorted(dates)  # (date, statement) order
    ids = {t["statement_id"] for t in txns}
    assert ids == {sid_blue, sid_copper}


def copper_txn_count():
    truth = json.loads((FIXTURES / "copperkettle_checking.truth.json")
                       .read_text())
    return len(truth["transactions"])


def test_chain_and_overlap_on_account_page(client):
    create(client, "Blue Lantern Savings")
    upload(client, "blue-lantern-savings", BLUE)
    upload(client, "blue-lantern-savings", COPPER)
    detail = client.get("/accounts/blue-lantern-savings",
                        headers=JSON).json()
    merged = client.get("/accounts/blue-lantern-savings/ledger.json").json()
    statements = merged["statements"]  # period order, like the account page
    ledgers = {}
    for s in statements:
        ledgers[s["id"]] = client.get(
            f"/accounts/blue-lantern-savings/statements/{s['id']}"
            "/ledger.json").json()
    breaks = sum(
        1 for prev, nxt in zip(statements, statements[1:])
        if abs(ledgers[prev["id"]]["closing_balance"]
               - ledgers[nxt["id"]]["opening_balance"]) > 0.01)
    chain = detail["chain"]
    assert chain["total"] == max(len(statements) - 1, 0)
    assert chain["ok"] == chain["total"] - breaks


def test_chain_pure_function():
    from web import ledger

    def meta(sid, start, end, opening, closing):
        return {"id": sid, "status": "parsed",
                "period": {"start": start, "end": end},
                "summary": {"opening_balance": opening,
                            "closing_balance": closing}}

    metas = [meta("a", "2026-01-01", "2026-01-31", 100.0, 150.0),
             meta("b", "2026-02-04", "2026-02-28", 150.0, 90.0),
             meta("c", "2026-02-25", "2026-03-31", 90.0, 60.0)]
    chain = ledger.chain(metas, tol=0.01)
    assert (chain["total"], chain["ok"]) == (2, 2)
    # b starts 3 days after a ends (gap); c starts inside b (overlap 4 days)
    assert ledger.period_warnings(metas) == [
        {"id": "b", "kind": "gap", "days": 3},
        {"id": "c", "kind": "overlap", "days": 4}]

    metas[1]["summary"]["opening_balance"] = 151.0  # break the chain
    chain = ledger.chain(metas, tol=0.01)
    assert chain["ok"] == 1 and chain["breaks"][0]["from_id"] == "a"

    csv_text = ledger.to_csv({"transactions": [
        {"date": "2026-01-02", "description": "Cafe, 'quotes'", "amount": -4.5,
         "balance": 95.5}]})
    assert csv_text.splitlines()[1] == ('2026-01-02,"Cafe, \'quotes\'",'
                                        '-4.50,95.50')


# --- Phase 3: harness trajectory, runs, pages ----------------------------------

import subprocess  # noqa: E402
import sys  # noqa: E402
import time as _time  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
CANNED_FULL = (REPO / "src" / "statementledger" / "registry"
               / f"{BLUE_KEY}.py")


@pytest.fixture()
def run_client(tmp_path):
    return TestClient(create_app(data_root=tmp_path / "data",
                                 harness_script=CANNED_FULL))


def _wait_terminal(client, run_id, timeout=90):
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        run = client.get(f"/runs/{run_id}", headers=JSON).json()
        if run["status"] in ("converged", "failed", "cancelled", "orphaned"):
            return run
        _time.sleep(0.3)
    pytest.fail(f"run {run_id} did not terminate")


def _launch(client, slug, ids, mode="draft", **extra):
    payload = {"account_slug": slug, "statement_ids": ids, "mode": mode}
    payload.update(extra)
    return client.post("/runs", json=payload, headers=JSON)


def test_run_converges_and_auto_parses(run_client, tmp_path):
    create(run_client, "Blue Lantern Savings", key=COPPER_KEY)
    sid = upload(run_client, "blue-lantern-savings", BLUE).json()["id"]
    response = _launch(run_client, "blue-lantern-savings", [sid])
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    run = _wait_terminal(run_client, run_id)
    assert run["status"] == "converged", run
    assert run["converged_key"] == BLUE_KEY
    # the pending statement was auto-parsed with the converged key
    assert run["auto_parse"] == [{"id": sid, "status": "parsed",
                                  "transactions": 14}]
    parsed = run_client.get(
        f"/accounts/blue-lantern-savings/statements/{sid}",
        headers=JSON).json()
    assert parsed["verified"] is True

    # the auto-parse pinned the exact parser revision (provenance §3.3)
    from statementledger import registry
    meta = json.loads((tmp_path / "data" / "accounts" / "blue-lantern-savings"
                       / "statements" / sid / "meta.json").read_text())
    head = registry.parser_history(
        BLUE_KEY, tmp_path / "data" / "registry")[-1]["sha16"]
    assert meta["history"][-1]["parser_sha"] == head

    # the trajectory is complete and relays over SSE with a run_status close
    stream = run_client.get(f"/runs/{run_id}/events")
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    payloads = [json.loads(line[6:]) for line in stream.text.splitlines()
                if line.startswith("data: ")]
    types = [event["type"] for event in payloads if "type" in event]
    assert types[0] == "run_started"   # model came from the trace's run start
    assert "gate" in types             # the canned gate
    assert types[-1] == "converged"    # terminal (run_status carries no type)
    assert run_client.get(f"/runs/{run_id}", headers=JSON).json()[
        "model"] == "canned script"

    # turn 0 (the canned zone) is browsable: staged code + gate verdict
    detail = run_client.get(f"/runs/{run_id}/turns/0").json()
    assert "def parse(" in detail["code"]
    assert detail["verdict"]["verified"]
    assert detail["summary"] is None or detail["notes"]


def test_run_saved_to_overlay_not_package(run_client, tmp_path):
    """Runtime-distilled parsers are USER DATA: the canned run must save
    into THIS app's registry overlay (its own data tree — pinned into the
    spawned agent), never into the package source."""
    create(run_client, "Blue Lantern Savings", key=COPPER_KEY)
    sid = upload(run_client, "blue-lantern-savings", BLUE).json()["id"]
    run_id = _launch(run_client, "blue-lantern-savings", [sid]).json()["run_id"]
    run = _wait_terminal(run_client, run_id)
    assert run["status"] == "converged"
    from statementledger import registry
    overlay = registry.user_registry(tmp_path / "data" / "registry")
    assert overlay is not None and (overlay / f"{BLUE_KEY}.py").exists()
    # and NOT the repo's dev data tree (the pre-pin poisoning path)
    assert not (REPO / "data" / "registry" / f"{BLUE_KEY}.py").exists()
    # the save left exactly one revision record, tied to THIS run: the
    # verified_against ids ARE the store's statement ids (pdf sha16s), and
    # the parent is the built-in the canned script copied
    history = registry.parser_history(BLUE_KEY, overlay)
    assert len(history) == 1
    assert history[0]["source"] == "distill"
    assert history[0]["run_id"] == run_id
    assert history[0]["verified_against"] == [sid]
    assert history[0]["parent_builtin_sha"] is not None


def test_run_validation_and_lock(run_client, tmp_path, monkeypatch):
    create(run_client, "Blue Lantern Savings", key=COPPER_KEY)
    sid = upload(run_client, "blue-lantern-savings", BLUE).json()["id"]
    # a REAL run (no canned script) requires the LLM endpoint configured.
    # The env-loader must stay out of it: a dev .env carrying the endpoint
    # would legitimately satisfy the check and flip the 400 to a 202.
    monkeypatch.delenv("STATEMENTLEDGER_LLM_URL", raising=False)
    monkeypatch.setattr("web.app._llm_env_loaded", True)
    plain = TestClient(create_app(data_root=tmp_path / "data-plain"))
    create(plain, "Blue Lantern Savings", key=COPPER_KEY)
    sid_plain = upload(plain, "blue-lantern-savings", BLUE).json()["id"]
    assert _launch(plain, "blue-lantern-savings", [sid_plain]).status_code == 400

    # with a canned script configured the env check is skipped (test seam)
    first = _launch(run_client, "blue-lantern-savings", [sid])
    assert first.status_code == 202
    second = _launch(run_client, "blue-lantern-savings", [sid])
    assert second.status_code == 409  # one active run at a time
    create(run_client, "No Pin Account")
    unpinned = upload(run_client, "no-pin-account", BLUE).json()["id"]
    assert _launch(run_client, "no-pin-account", [unpinned],
                   mode="maintain").status_code == 400  # no key to maintain
    assert _launch(run_client, "nope-account", [sid]).status_code == 404
    run_client.delete(f"/runs/{first.json()['run_id']}")
    _wait_terminal(run_client, first.json()["run_id"])


def test_launch_maintain_defaults_to_pin(run_client):
    # pin the bank's own parser: maintain targets the pin with no explicit
    # key, the staged baseline already verifies, and the run converges with
    # nothing to do (the canned BLUE script would fail the key-pin check —
    # maintain mode refuses a parser that renames the key)
    create(run_client, "Blue Lantern Savings", key=BLUE_KEY)
    sid = upload(run_client, "blue-lantern-savings", BLUE).json()["id"]
    response = _launch(run_client, "blue-lantern-savings", [sid],
                       mode="maintain")  # no explicit key: use the pin
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    run = _wait_terminal(run_client, run_id)
    assert run["key"] == BLUE_KEY  # maintain targeted the pinned parser
    assert run["status"] == "converged"  # baseline already verifies
    # the baseline (turn 0) is the staged registry source, gated up front
    detail = run_client.get(f"/runs/{run_id}/turns/0")
    assert detail.status_code == 200
    assert "def parse(" in detail.json()["code"]
    assert detail.json()["verdict"]["verified"]


def test_run_cancel(tmp_path):
    # a canned parser whose import sleeps inside the sandboxed gate: the run
    # stays alive long enough to be cancelled mid-gate
    slow = tmp_path / "slow_parser.py"
    source = CANNED_FULL.read_text()
    slow.write_text("import time as _t\n_t.sleep(50)\n" + source)
    client = TestClient(create_app(data_root=tmp_path / "data-slow",
                                   harness_script=slow))
    create(client, "Blue Lantern Savings", key=COPPER_KEY)
    sid = upload(client, "blue-lantern-savings", BLUE).json()["id"]
    run_id = _launch(client, "blue-lantern-savings", [sid],
                     turns=500).json()["run_id"]
    _time.sleep(1.5)  # let the gate child reach its sleep
    cancel = client.delete(f"/runs/{run_id}")
    assert cancel.status_code == 202
    run = _wait_terminal(client, run_id)
    assert run["status"] == "cancelled"
    events = client.get(f"/runs/{run_id}/events").text
    assert "cancelled" in events
    # a cancelled run frees the one-run lock
    assert _launch(client, "blue-lantern-savings", [sid],
                   turns=1).status_code == 202


def test_attach_to_existing_run(run_client):
    result = subprocess.run(  # a real agent run, no trajectory flag
        [sys.executable, "tools/distill_agent.py", str(BLUE),
         "--script", str(CANNED_FULL), "--no-save"],
        cwd=REPO, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0
    workdir = next(line.split(": ", 1)[1] for line in result.stdout.splitlines()
                   if line.startswith("workdir: "))
    create(run_client, "Blue Lantern Savings")
    response = _launch(run_client, "blue-lantern-savings", [], workdir=workdir)
    assert response.status_code == 202
    run = _wait_terminal(run_client, response.json()["run_id"])
    assert run["status"] == "converged"  # derived from the trace's result
    assert run["converged_key"] == BLUE_KEY
    bad = _launch(run_client, "blue-lantern-savings", [],
                  workdir="logs/definitely-not-there")
    assert bad.status_code == 400


SPA_INDEX = (Path(__file__).resolve().parents[1] / "web" / "frontend"
             / "dist" / "index.html")


@pytest.mark.skipif(not SPA_INDEX.exists(), reason="SPA bundle not built")
def test_spa_shell_served(run_client):
    """Browsers get the SPA shell on page routes and unknown GETs (history
    fallback); API clients get JSON; legacy /app links redirect to /."""
    create(run_client, "Blue Lantern Savings", key=COPPER_KEY)
    sid = upload(run_client, "blue-lantern-savings", BLUE).json()["id"]
    run_id = _launch(run_client, "blue-lantern-savings", [sid]).json()["run_id"]
    _wait_terminal(run_client, run_id)
    shell = run_client.get(f"/runs/{run_id}")
    assert shell.status_code == 200 and 'id="root"' in shell.text
    # unknown paths: browsers get the SPA shell (history fallback), API
    # clients a JSON 404; malformed statement ids are 400s, not 500s
    fallback = run_client.get("/some/unknown/path")
    assert 'id="root"' in fallback.text
    api404 = run_client.get("/nope", headers=JSON)
    assert api404.status_code == 404 and api404.json()["detail"]
    missing = run_client.get(
        "/accounts/blue-lantern-savings/statements/" + "0" * 16, headers=JSON)
    assert missing.status_code == 404
    bad = run_client.get(
        "/accounts/blue-lantern-savings/statements/0000", headers=JSON)
    assert bad.status_code == 400
    legacy = run_client.get(f"/app/runs/{run_id}", follow_redirects=False)
    assert legacy.status_code == 308
    assert legacy.headers["location"] == f"/runs/{run_id}"


def test_statement_page_raster_and_frames(client):
    create(client, "Blue Lantern Savings")
    sid = upload(client, "blue-lantern-savings", BLUE).json()["id"]
    png = client.get(f"/accounts/blue-lantern-savings/statements/{sid}"
                     "/pages/0.png")
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"
    missing = client.get(f"/accounts/blue-lantern-savings/statements/{sid}"
                         "/pages/99.png")
    assert missing.status_code == 404
    frames = client.get(f"/accounts/blue-lantern-savings/statements/{sid}"
                        "/frames.json")
    assert frames.status_code == 200
    assert frames.json()["lines"]  # overlay geometry available


def test_prov_pages_geometry():
    from web.app import _prov_pages

    lines = json.loads(
        (FIXTURES / "bluelantern_savings.frames.json").read_text()
    )["statements"][0]["lines"]
    frames = {"name": "x.pdf", "lines": lines}
    ledger = {"_anchors": {"opening_balance": 2}, "transactions": [
        {"date": "2026-03-01", "description": "a", "amount": 1, "balance": 2,
         "line": 4}]}
    pages = _prov_pages(ledger, frames)
    assert [p["page"] for p in pages] == [0]
    bands = pages[0]["bands"]
    assert {b["kind"] for b in bands} == {"anchor", "txn"}
    txn_band = next(b for b in bands if b["kind"] == "txn")
    assert txn_band["left"] == min(item[1] for item in lines[4]["items"]) * 2
    assert _prov_pages(ledger, None) == []
    assert _prov_pages({"transactions": []}, frames) == []


# --- SPA API: JSON content-negotiation branches --------------------------------


def test_spa_json_branches(client):
    # index → compact account rows
    assert client.get("/", headers=JSON).json() == {"accounts": []}
    create(client, "Blue Lantern Savings", key=BLUE_KEY)
    upload(client, "blue-lantern-savings", BLUE)
    rows = client.get("/", headers=JSON).json()["accounts"]
    assert rows[0]["account"]["slug"] == "blue-lantern-savings"
    assert rows[0]["total"] == 1 and rows[0]["parsed"] == 1
    assert rows[0]["latest"]["summary"]["transactions"] == 14

    # account detail → metas/chain/warnings/runs/model
    detail = client.get("/accounts/blue-lantern-savings", headers=JSON).json()
    assert detail["account"]["registry_key"] == BLUE_KEY
    assert len(detail["metas"]) == 1
    assert "chain" in detail and "warnings" in detail and "runs" in detail

    # create via JSON → 201 + account body
    made = create(client, "Another Bank")
    assert made.status_code == 201
    assert made.json()["slug"] == "another-bank"

    # set key via JSON → account body
    repin = client.post("/accounts/another-bank/key",
                        data={"registry_key": COPPER_KEY}, headers=JSON)
    assert repin.status_code == 200
    assert repin.json()["registry_key"] == COPPER_KEY


def test_spa_statement_and_run_json(run_client):
    create(run_client, "Blue Lantern Savings", key=COPPER_KEY)
    sid = upload(run_client, "blue-lantern-savings", BLUE).json()["id"]
    detail = run_client.get(
        f"/accounts/blue-lantern-savings/statements/{sid}",
        headers=JSON).json()
    assert detail["meta"]["id"] == sid
    assert detail["ledger"] is None  # wrong pin: failed, nothing recorded
    assert detail["verified"] is None

    run_id = _launch(run_client, "blue-lantern-savings", [sid]).json()["run_id"]
    _wait_terminal(run_client, run_id)
    body = run_client.get(f"/runs/{run_id}", headers=JSON).json()
    assert body["display_name"] == "Blue Lantern Savings"
    assert body["sid"] == sid and body["pages"] >= 1
    assert body["status"] == "converged"
    # parsed statement: verified flag; the demo parser doesn't adopt the
    # provenance convention (txn["line"]/_anchors), so the overlay degrades
    # to an empty list — distilled parsers that do adopt it get geometry
    parsed = run_client.get(
        f"/accounts/blue-lantern-savings/statements/{sid}",
        headers=JSON).json()
    assert parsed["verified"] is True
    assert parsed["prov_pages"] == []


def test_agent_events_tailed_verbatim_via_attach(run_client, tmp_path):
    """Current agents emit events.jsonl themselves; the manager relays those
    lines untouched (no derivation). Attach mode over a synthetic workdir
    exercises the exact tail/pump/relay code path with zero subprocesses —
    the agent-side format guarantee lives in
    test_distill_agent.test_emitted_events_match_the_derived_trace."""
    import shutil
    import uuid

    events = [
        {"type": "run_started", "ts": "2026-10-10T22:00:00+00:00",
         "model": "canned script"},
        {"type": "frames", "ts": "2026-10-10T22:00:00+00:00",
         "statements": 1, "name": "x.pdf", "lines": 40},
        {"type": "tool", "ts": "2026-10-10T22:00:01+00:00", "turn": 1,
         "name": "grep", "args": {"pattern": "stop"},
         "result": '{"hits": [[3, "Transactions stop"]], "count": 1}'},
        {"type": "look", "ts": "2026-10-10T22:00:01+00:00", "turn": 1,
         "tool": "grep", "label": "grep stop", "page": 0,
         "rect": [20, 88, 300, 101], "line_ids": [3]},
        {"type": "gate", "ts": "2026-10-10T22:00:02+00:00", "turn": 0,
         "baseline": True, "verdict": {"verified": True, "clean": 1}},
        {"type": "converged", "ts": "2026-10-10T22:00:02+00:00",
         "key": "us__x__checking__1", "saved": True},
    ]
    # the attach guard requires workdirs under the repo root; logs/ is the
    # real agent workdir tree (gitignored) — nest there, clean up after
    workdir = REPO / "logs" / f"synthetic_{uuid.uuid4().hex[:8]}"
    workdir.mkdir(parents=True)
    try:
        with (workdir / "events.jsonl").open("w") as out:
            for event in events:
                out.write(json.dumps(event) + "\n")
        create(run_client, "Blue Lantern Savings")
        run_id = _launch(run_client, "blue-lantern-savings", [],
                         workdir=str(workdir)).json()["run_id"]
        run = _wait_terminal(run_client, run_id)
        assert run["status"] == "converged"
        assert run["converged_key"] == "us__x__checking__1"

        stream = run_client.get(f"/runs/{run_id}/events").text
        relayed = [json.loads(line[6:]) for line in stream.splitlines()
                   if line.startswith("data: ")]
        relayed = [event for event in relayed
                   if "type" in event]  # drop the closing run_status frame
        assert relayed == events  # verbatim: identity, not re-derivation
        detail = run_client.get(f"/runs/{run_id}/turns/1").json()
        assert detail["tools"][0]["name"] == "grep"  # turn detail consumes
        # the same events: one feed, no separate log parsing
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
