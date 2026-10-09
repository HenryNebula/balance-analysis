"""The registry end to end: every registered parser verifies its fixture,
matches ground truth exactly, and the public parse() dispatch works both by
key and by discovery."""

import json
from pathlib import Path

import pytest

import statementledger as sl

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
