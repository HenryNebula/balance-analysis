"""Web UI Phase 1 (docs/web-ui-design.md §11): accounts, upload + sync parse
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
    page = client.get("/")
    assert page.status_code == 200
    assert "No accounts yet" in page.text


def test_create_account(client, tmp_path):
    response = create(client, "Blue Lantern Savings")
    assert response.status_code in (200, 303)
    account_file = (tmp_path / "data" / "accounts" / "blue-lantern-savings"
                    / "account.json")
    account = json.loads(account_file.read_text())
    assert account["registry_key"] is None
    assert account["slug"] == "blue-lantern-savings"
    assert client.get("/").text.count('href="/accounts/blue-lantern-savings"')


def test_create_account_duplicate_slug(client):
    assert create(client, "Blue Lantern Savings").status_code in (200, 303)
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
    assert response.status_code == 200  # domain outcome, not an error (§4)
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

    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    canvas.Canvas(buffer).save()  # valid one-page PDF, zero text
    create(client, "Blue Lantern Savings")
    response = client.post(
        "/accounts/blue-lantern-savings/statements",
        files={"file": ("scan.pdf", buffer.getvalue(), "application/pdf")},
        headers=JSON)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["attempts"] == []
    assert "scanned" in body["note"]


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
    page = client.get(f"/accounts/blue-lantern-savings/statements/{sid}")
    assert page.status_code == 200
    assert "RECONCILED" in page.text
    truth = json.loads((FIXTURES / "bluelantern_savings.truth.json").read_text())
    assert truth["transactions"][0]["description"] in page.text
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
    assert "no parser verified" in client.get(
        f"/accounts/blue-lantern-savings/statements/{sid}").text.lower()
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
                           data={"registry_key": ""}, headers=JSON,
                           follow_redirects=False)
    assert response.status_code in (200, 303)
    page = client.get("/accounts/blue-lantern-savings")
    assert "detection mode" in page.text


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


def test_registry(client):
    create(client, "Blue Lantern Savings")
    upload(client, "blue-lantern-savings", BLUE)
    data = client.get("/registry.json").json()
    keys = {row["key"]: row for row in data}
    assert BLUE_KEY in keys and keys[BLUE_KEY]["description"]
    import statementledger as sl
    assert set(keys) == set(sl.keys())  # every module, docstring or not
    page = client.get("/registry")
    assert page.status_code == 200
    assert BLUE_KEY in page.text
    assert COPPER_KEY in page.text


def test_upload_returns_htmx_fragment(client):
    create(client, "Blue Lantern Savings")
    response = upload(client, "blue-lantern-savings", BLUE,
                      headers={"hx-request": "true"})
    assert response.status_code == 201
    assert 'id="statements-region"' in response.text
    assert "✓ Parsed" in response.text
    assert BLUE_KEY in response.text


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


def test_probe_fragment_has_pin_picker(client):
    create(client, "Blue Lantern Savings", key=COPPER_KEY)
    sid = upload(client, "blue-lantern-savings", BLUE).json()["id"]
    page = client.post("/accounts/blue-lantern-savings/probe",
                       json={"statement_id": sid},
                       headers={"hx-request": "true"})
    assert "Pin to" in page.text and BLUE_KEY in page.text


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
    page = client.get("/accounts/blue-lantern-savings")
    merged = client.get("/accounts/blue-lantern-savings/ledger.json").json()
    statements = merged["statements"]  # period order, like the page
    breaks = 0
    ledgers = {}
    for s in statements:
        sid = s["id"]
        ledgers[sid] = client.get(
            f"/accounts/blue-lantern-savings/statements/{sid}"
            "/ledger.json").json()
    for prev, nxt in zip(statements, statements[1:]):
        if abs(ledgers[prev["id"]]["closing_balance"]
               - ledgers[nxt["id"]]["opening_balance"]) > 0.01:
            breaks += 1
    links = max(len(statements) - 1, 0)
    if breaks == 0 and links:
        assert f"chain ✓ ({links}/{links} links)" in page.text
    else:
        assert "chain break" in page.text


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
