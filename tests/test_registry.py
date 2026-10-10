"""The registry end to end: every registered parser verifies its fixture,
matches ground truth exactly, and the public parse() dispatch works both by
key and by discovery."""

import json
from pathlib import Path

import pytest

import statementledger as sl
from statementledger.registry import key_problem

FIXTURES = Path(__file__).parent / "fixtures"
CASES = [
    ("copperkettle_checking.pdf", "us__copperkettle__checking__1"),
    ("bluelantern_savings.pdf", "us__bluelantern__savings__1"),
]


def _frames(pdf_name):
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        return sl.frames_for([FIXTURES / pdf_name], Path(tmp))["statements"]


@pytest.mark.parametrize("pdf,key", CASES)
def test_parser_verifies(pdf, key):
    module = sl.load_module(key)
    verdict = sl.evaluate(module.parse, _frames(pdf), key=key)
    assert verdict["verified"], verdict["results"]


@pytest.mark.parametrize("pdf,key", CASES)
def test_parser_matches_ground_truth(pdf, key):
    module = sl.load_module(key)
    truth = json.loads((FIXTURES / pdf.replace(".pdf", ".truth.json")).read_text())
    got = module.parse(_frames(pdf)[0])
    assert got == truth


@pytest.mark.parametrize("pdf,key", CASES)
def test_public_parse_by_key(pdf, key):
    stmt = sl.parse(str(FIXTURES / pdf), key=key)
    assert stmt["_key"] == key
    assert sl.reconcile_statement(stmt) is None


@pytest.mark.parametrize("pdf,key", CASES)
def test_public_parse_discovery(pdf, key):
    stmt = sl.parse(str(FIXTURES / pdf))  # no key: first verifying parser wins
    assert stmt["_key"] == key


def test_cross_fixture_no_false_positive():
    # one bank's parser must not verify the other bank's statement
    module = sl.load_module("us__bluelantern__savings__1")
    verdict = sl.evaluate(module.parse, _frames("copperkettle_checking.pdf"))
    assert not verdict["verified"]


def test_unregistered_key_is_an_error():
    with pytest.raises((ValueError, FileNotFoundError)):
        sl.load_module("us__nosuch__bank__product__9")


def test_user_registry_overlay(tmp_path, monkeypatch):
    # parsers distilled at runtime are USER DATA: an overlay directory that
    # keys() unions in, load_module() shadows built-ins with, and
    # module_path() points saves at — nothing lands in the package source
    from statementledger import registry

    monkeypatch.setenv("STATEMENTLEDGER_USER_REGISTRY", str(tmp_path))
    (tmp_path / "us__overlaybank__savings__1.py").write_text(
        'KEY = "us__overlaybank__savings__1"\n'
        "def parse(frames):\n    return {}\n")
    assert "us__overlaybank__savings__1" in sl.keys()
    assert registry.module_path("us__newbank__savings__1") == (
        tmp_path / "us__newbank__savings__1.py")

    # a same-key overlay module shadows the built-in (maintain-mode evolution
    # writes a user copy, the shipped source stays untouched)
    shadow = tmp_path / "us__bluelantern__savings__1.py"
    shadow.write_text(
        'KEY = "us__bluelantern__savings__1"\n'
        "def parse(frames):\n"
        "    return {'sentinel': 'overlay'}\n")
    assert registry.module_path("us__bluelantern__savings__1") == shadow
    assert sl.load_module("us__bluelantern__savings__1").parse({}) == {
        "sentinel": "overlay"}

    monkeypatch.delenv("STATEMENTLEDGER_USER_REGISTRY")
    assert "us__overlaybank__savings__1" not in sl.keys()  # gone when unset
    assert registry.module_path("us__bluelantern__savings__1") == (
        registry.REGISTRY / "us__bluelantern__savings__1.py")


@pytest.mark.parametrize("pdf,key", CASES)
def test_public_parse_all_normalizes_single_ledger(pdf, key):
    ledgers = sl.parse_all(str(FIXTURES / pdf), key=key)
    assert isinstance(ledgers, list) and len(ledgers) == 1
    assert ledgers[0]["_key"] == key
    assert sl.reconcile_statement(ledgers[0]) is None


# --- key_problem: the gate that keeps template placeholders out of the registry

@pytest.mark.parametrize("key,fragment", [
    # the shipped bug: KEY_RE passes but '__' splits into 5 segments
    ("us__bank__liberty_national__credit__1", "segments"),
    # unreplaced template bank / filler product segments
    ("us__bank__checking__1", "placeholder"),
    ("ca__harbour__bank__1", "placeholder"),
    ("us__demo__checking__1", "placeholder"),
    # shape failures
    ("US__Bank__Checking__1", "lowercase"),
    ("us__copperkettle__checking", "want cc__bank__product__n"),
    # real keys pass
    ("us__copperkettle__checking__1", None),
    ("sg__standard_chartered__savings__1", None),
    ("ca__harbour_bank__business__1", None),
])
def test_key_problem(key, fragment):
    problem = key_problem(key)
    if fragment is None:
        assert problem is None
    else:
        assert problem and fragment in problem


def test_placeholder_key_cannot_load():
    with pytest.raises(ValueError, match="placeholder"):
        sl.load_module("us__bank__checking__1")
