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
