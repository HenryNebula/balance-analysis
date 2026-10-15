"""The registry end to end: every registered parser verifies its fixture,
matches ground truth exactly, and the public parse() dispatch works both by
key and by discovery."""

import json
from pathlib import Path

import pytest

import statementledger as sl
from statementledger import registry
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
    # no env → the XDG default overlay (pinned to tmp by conftest): saves
    # never fall back into package source anymore
    assert registry.module_path("us__bluelantern__savings__1") == (
        tmp_path / "xdg" / "statementledger" / "registry"
        / "us__bluelantern__savings__1.py")


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


# --- revision history: content-addressed blobs + append-only history.jsonl

SCRATCH = "us__scratchbank__savings__1"


def _code(marker=""):
    return (f'KEY = "{SCRATCH}"\n'
            "def parse(frames):\n"
            f"    return {{'marker': '{marker}'}}\n")


def _sha16(text):
    from hashlib import sha256

    return sha256(text.encode()).hexdigest()[:16]


def test_default_overlay_resolution(tmp_path):
    # env unset (conftest) + XDG_DATA_HOME pinned per-test → the default
    # overlay; a save lands live copy + .history there, package source untouched
    default = tmp_path / "xdg" / "statementledger" / "registry"
    assert registry.user_registry() == default
    sha = registry.save_parser(SCRATCH, _code(), source="distill")
    assert (default / f"{SCRATCH}.py").read_text() == _code()
    assert (default / ".history" / SCRATCH / "objects" / f"{sha}.py").exists()
    assert not (registry.REGISTRY / f"{SCRATCH}.py").exists()


def test_save_history_roundtrip(tmp_path):
    v1, v2 = _code("one"), _code("two")
    sha1 = registry.save_parser(SCRATCH, v1, tmp_path, source="distill",
                                verified_against=("3f9c1a2b4d5e6f70",))
    sha2 = registry.save_parser(SCRATCH, v2, tmp_path, source="maintain",
                                run_id="20261014-075959-a1b2")
    history = registry.parser_history(SCRATCH, tmp_path)
    assert [r["sha16"] for r in history] == [sha1, sha2]  # oldest first
    assert history[0]["source"] == "distill"
    assert history[0]["verified_against"] == ["3f9c1a2b4d5e6f70"]
    assert history[1]["run_id"] == "20261014-075959-a1b2"
    assert (tmp_path / f"{SCRATCH}.py").read_text() == v2  # live = head
    assert registry.revision_code(SCRATCH, sha1, tmp_path) == v1
    registry.revert_parser(SCRATCH, sha1, tmp_path, note="back to v1")
    assert (tmp_path / f"{SCRATCH}.py").read_text() == v1
    assert registry.parser_history(SCRATCH, tmp_path)[-1]["source"] == "revert"


def test_save_dedupes_identical_head(tmp_path):
    code = _code()
    sha_a = registry.save_parser(SCRATCH, code, tmp_path, source="distill")
    (tmp_path / f"{SCRATCH}.py").write_text(code + "# hand edit\n")  # drift
    sha_b = registry.save_parser(SCRATCH, code, tmp_path, source="distill")
    assert sha_a == sha_b
    assert len(registry.parser_history(SCRATCH, tmp_path)) == 1  # no dupe
    assert (tmp_path / f"{SCRATCH}.py").read_text() == code  # drift healed


def test_history_invisible_to_keys(tmp_path):
    registry.save_parser(SCRATCH, _code(), tmp_path, source="distill")
    assert SCRATCH in registry.keys(tmp_path)
    assert all(not k.startswith(".") for k in registry.keys(tmp_path))
    # a nonexistent overlay dir is glob-safe (built-ins still listed)
    assert SCRATCH not in registry.keys(tmp_path / "nope")


def test_shadow_save_records_parent_builtin_sha(tmp_path):
    # first overlay save of a shipped parser records the built-in it forks
    # from; later saves don't
    key = "us__bluelantern__savings__1"
    builtin = (registry.REGISTRY / f"{key}.py").read_text()
    registry.save_parser(key, builtin + "# evolved\n", tmp_path,
                         source="maintain")
    registry.save_parser(key, builtin + "# evolved more\n", tmp_path,
                         source="maintain")
    history = registry.parser_history(key, tmp_path)
    assert history[0]["parent_builtin_sha"] == _sha16(builtin)
    assert history[1]["parent_builtin_sha"] is None


def test_current_sha(tmp_path):
    key = "us__bluelantern__savings__1"
    builtin = (registry.REGISTRY / f"{key}.py").read_text()
    assert registry.current_sha(key, tmp_path) == _sha16(builtin)  # fallback
    evolved = builtin + "# evolved\n"
    registry.save_parser(key, evolved, tmp_path, source="maintain")
    assert registry.current_sha(key, tmp_path) == _sha16(evolved)  # shadow
    assert registry.current_sha("us__nosuchbank__checking__9",
                                tmp_path) is None


def test_parser_history_skips_corrupt_lines(tmp_path):
    assert registry.parser_history(SCRATCH, tmp_path) == []  # no dir yet
    registry.save_parser(SCRATCH, _code(), tmp_path, source="distill")
    log = tmp_path / ".history" / SCRATCH / "history.jsonl"
    log.write_text("")  # empty file
    assert registry.parser_history(SCRATCH, tmp_path) == []
    log.write_text('{"at": "x", "sha16": "abc"}\nnot json at all\n\n')
    assert registry.parser_history(SCRATCH, tmp_path) == [
        {"at": "x", "sha16": "abc"}]


def test_revert_to_head_is_noop(tmp_path):
    sha = registry.save_parser(SCRATCH, _code(), tmp_path, source="distill")
    registry.revert_parser(SCRATCH, sha, tmp_path)
    assert len(registry.parser_history(SCRATCH, tmp_path)) == 1


def test_discovery_skips_unloadable_overlay_modules(tmp_path, monkeypatch):
    # a junk overlay entry (here: an unreplaced placeholder key — a real
    # artifact found in a live overlay) is a failed candidate, not a dead
    # discovery loop: the right parser still wins
    monkeypatch.setenv("STATEMENTLEDGER_USER_REGISTRY", str(tmp_path))
    (tmp_path / "us__bank__product__1.py").write_text(
        'KEY = "us__bank__product__1"\n'
        "def parse(frames):\n    return {}\n")
    assert "us__bank__product__1" in sl.keys()
    stmt = sl.parse(str(FIXTURES / "copperkettle_checking.pdf"))
    assert stmt["_key"] == "us__copperkettle__checking__1"
