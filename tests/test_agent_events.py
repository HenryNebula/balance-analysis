"""The trace.log → trajectory derivation (web/agent_events.py): pure fold
over recorded agent traces — the web run view's whole event vocabulary
comes from here, so every section shape the agent writes gets a test."""

import json
from pathlib import Path

from web.agent_events import TraceDeriver

FIXTURES = Path(__file__).parent / "fixtures"
RULE = "=" * 78
DASH = "-" * 78


def section(name: str, body: str = "") -> str:
    return f"\n{RULE}\n[10:00:00] {name}\n{DASH}\n{body}\n"


def frames_lines():
    return json.loads(
        (FIXTURES / "bluelantern_savings.frames.json").read_text()
    )["statements"][0]["lines"]


def types(events):
    return [event["type"] for event in events]


def test_full_agent_arc():
    """The canonical live arc: look → write → gate fails → edit → gate
    VERIFIED → turn lines → result saved."""
    code_v1 = 'KEY = "us__demo__bank__1"\ndef parse(frames):\n    return {}\n'
    old = "    return {}"
    new = "    return {'transactions': []}"
    code_v2 = code_v1.replace(old, new)
    trace = "".join([
        section("run start", "mode=qwen3.5-4b at http://127.0.0.1:30000/v1\n"
                            "pdfs=['a.pdf']"),
        section("frames", "1 statement(s); a.pdf: 120 lines"),
        section("tool call: render", '{"start": 0, "n": 40}'),
        section("tool result: render", "[Page 0]\n000 | hello"),
        "turn 1: look · 6k chars\n",
        section("tool call: write", json.dumps({"content": code_v1})),
        section("write parser.py", code_v1),
        section("tool result: write",
                f"wrote parser.py ({len(code_v1.splitlines())} lines)"),
        section("tool call: run", "{}"),
        section("tool result: run", "0/1 statements reconcile..."),
        section("parser.py statementledger gate",
                "exit 0\n" + json.dumps({
                    "key": "us__demo__bank__1", "stage": "parse", "clean": 0,
                    "verified": False,
                    "results": [{"name": "a.pdf", "ok": False, "rows": 3,
                                 "error": "reconcile broke"}]})),
        "turn 2: 0/1 clean · 9k chars\n",
        section("tool call: edit",
                json.dumps({"old": old, "new": new})),
        section("edit parser.py", f"- {old!r}\n+ {new!r}"),
        section("tool result: edit", "replaced 1 occurrence"),
        section("tool call: run", "{}"),
        section("tool result: run", "VERIFIED — reconciles every statement"),
        section("parser.py statementledger gate",
                "exit 0\n" + json.dumps({
                    "key": "us__demo__bank__1", "stage": "parse", "clean": 1,
                    "verified": True, "results": [
                        {"name": "a.pdf", "ok": True, "rows": 3}]})),
        "turn 3: VERIFIED via run\n",
        section("result", "VERIFIED parser saved: /x/registry/us__demo__bank__1.py"),
    ])

    deriver = TraceDeriver()
    events = deriver.feed(trace)
    events += deriver.close()  # the manager's final pump flushes the tail

    assert types(events)[:3] == ["run_started", "frames", "tool"]
    assert events[0]["model"] == "qwen3.5-4b"
    tools = [e for e in events if e["type"] == "tool"]
    assert [t["name"] for t in tools] == ["render", "write", "run",
                                          "edit", "run"]
    assert tools[0]["args"] == {"start": 0, "n": 40}
    # the write snapshot, then the edit replayed on top of it
    snapshots = [e for e in events if e["type"] == "parser"]
    assert [s["turn"] for s in snapshots] == [2, 3]
    assert snapshots[0]["content"] == code_v1
    assert snapshots[1]["content"] == code_v2
    assert deriver.parser_code == code_v2
    # gate verdicts parsed as JSON, attributed to their turns
    gates = [e for e in events if e["type"] == "gate"]
    assert [g["turn"] for g in gates] == [2, 3]
    assert gates[0]["verdict"]["verified"] is False
    assert gates[1]["verdict"]["verified"] is True
    # turn summaries; the VERIFIED turn closes via its bare line
    turns = [e for e in events if e["type"] == "turn"]
    assert [(t["turn"], t["summary"]) for t in turns] == [
        (1, "look"), (2, "0/1 clean"), (3, "VERIFIED")]
    assert turns[1]["chars_k"] == 9
    # terminal
    terminal = {k: v for k, v in deriver.terminal.items() if k != "ts"}
    assert terminal == {"type": "converged",
                        "key": "us__demo__bank__1", "saved": True}
    assert deriver.key_hint() == "us__demo__bank__1"


def test_incremental_feed_and_torn_tail():
    """The manager tails byte-by-byte: a split mid-section must not
    duplicate or lose events, and a torn tail line waits for its rest."""
    part_one = (section("run start", "mode=m at http://x/v1") +
                section("tool call: grep", '{"pattern": "Date"}')[:40])
    part_two = (section("tool call: grep", '{"pattern": "Date"}')[40:] +
                section("tool result: grep",
                        '{"hits": [[3, "01/02/2025"]], "count": 1}') +
                "turn 1: lo")
    part_three = "ok · 2k chars\n"
    deriver = TraceDeriver()
    first = deriver.feed(part_one)
    assert types(first) == []  # run start stays open until its closer line
    second = deriver.feed(part_two)
    assert types(second) == ["run_started"]  # the next head flushed it; the
    # grep RESULT is now the open section (its closer is still torn)
    third = deriver.feed(part_three)
    assert types(third) == ["tool", "turn"]  # the turn line closed the result
    assert third[1]["summary"] == "look"
    assert third[1]["chars_k"] == 2


def test_look_events_resolve_rects():
    """render/grep line spans resolve against frames into overlay rects —
    the PDF pane's selection (pdfplumber points, y downward)."""
    lines = frames_lines()
    deriver = TraceDeriver(lines)
    events = deriver.feed("".join([
        section("run start", "mode=m at http://x"),
        section("tool call: grep", '{"pattern": "Date"}'),
        section("tool result: grep",
                json.dumps({"hits": [[3, "01/02/2025"], [5, "03/04/2025"]],
                            "count": 2})),
        section("tool call: render", '{"start": 10, "n": 4}'),
        section("tool result: render", "[Page 0]\n010 | x"),
        "turn 1: look · 3k chars\n",
    ]))
    looks = [e for e in events if e["type"] == "look"]
    assert [l["tool"] for l in looks] == ["grep", "render"]
    grep, render = looks
    assert grep["line_ids"] == [3, 5]
    assert grep["page"] == lines[3]["page"]
    expected_x1 = min(item[1] for i in (3, 5) for item in lines[i]["items"])
    expected_x2 = max(item[2] for i in (3, 5) for item in lines[i]["items"])
    assert grep["rect"][0] == expected_x1
    assert grep["rect"][2] == expected_x2
    assert grep["rect"][1] < grep["rect"][3]
    assert render["line_ids"] == [10, 11, 12, 13]
    # a look at a non-first statement resolves to nothing (single-pane UI)
    other = deriver.feed(section("tool call: render", '{"stmt": 1}') +
                         section("tool result: render", "[Page 0]"))
    assert "look" not in types(other)


def test_baseline_and_maintain_zone():
    """Maintain mode gates the staged parser BEFORE any tool call: that gate
    is turn 0, flagged baseline; coverage pins land as turn-0 notes."""
    deriver = TraceDeriver()
    events = deriver.feed("".join([
        section("run start", "mode=m at http://x"),
        section("parser.py statementledger gate",
                "exit 0\n" + json.dumps({
                    "key": "k__b__p__1", "clean": 1, "verified": False,
                    "results": [{"name": "old.pdf", "ok": True, "rows": 5}]})),
        section("coverage pins", '{"old.pdf": 5}'),
        section("tool call: edit", '{"old": "a", "new": "b"}'),
        section("tool result: edit", "edited"),
    ]))
    events += deriver.close()
    gate = next(e for e in events if e["type"] == "gate")
    assert gate["turn"] == 0 and gate.get("baseline") is True
    tool = next(e for e in events if e["type"] == "tool")
    assert tool["turn"] == 1  # after turn 0, sections belong to turn 1


def test_notes_and_terminal_flavours():
    """Duplicate/refused bare lines attach to their turn; loop abort and
    the not-saved result section round out the vocabulary."""
    deriver = TraceDeriver()
    events = deriver.feed("".join([
        section("turn 4 chat failed", "HTTP 400: too big"),
        "turn 4: duplicate grep call (first at turn 2)\n",
        "turn 5: REFUSED duplicate python call (x3)\n",
        "turn 5: loop abort — 6 consecutive turns of refused duplicate calls\n",
        section("turn 6 context elided", "12000 chars saved"),
        section("result", "FAILED (not verified)"),
    ]))
    events += deriver.close()
    kinds = types(events)
    assert "chat_failed" in kinds and "elided" in kinds
    elided = next(e for e in events if e["type"] == "elided")
    assert elided == {"type": "elided", "turn": 6, "chars": 12000,
                      "forced": False, "ts": elided["ts"]}
    abort = next(e for e in events if e["type"] == "turn"
                 and e["summary"] == "loop abort")
    assert abort["turn"] == 5
    assert any("REFUSED" in note for note in abort["notes"])
    assert deriver.terminal["type"] == "failed"

    not_saved = TraceDeriver()
    draft = 'KEY = "us__x__y__1"\ndef parse(f):\n    pass\n'
    not_saved.feed(section("write parser.py", draft) +
                   section("result", "VERIFIED (not saved); parser drafted in logs/d_1"))
    not_saved.close()
    terminal = {k: v for k, v in not_saved.terminal.items() if k != "ts"}
    assert terminal == {"type": "converged", "key": "us__x__y__1",
                        "saved": False}


def test_canned_run_derives_converged():
    """The canned seam (tests, attach): staged script, one gate, result."""
    deriver = TraceDeriver()
    events = deriver.feed("".join([
        section("run start", "mode=canned script /tmp/x.py"),
        section("frames", "1 statement(s); b.pdf: 40 lines"),
        section("parser.py statementledger gate",
                "exit 0\n" + json.dumps({
                    "key": "us__x__y__1", "clean": 1, "verified": True,
                    "results": [{"name": "b.pdf", "ok": True, "rows": 4}]})),
        section("canned run", "VERIFIED — reconciles every statement"),
        section("result", "VERIFIED parser saved: /r/us__x__y__1.py"),
    ]))
    events += deriver.close()
    assert deriver.model == "canned script"
    gate = next(e for e in events if e["type"] == "gate")
    assert gate["turn"] == 0 and gate["verdict"]["verified"]
    assert deriver.terminal["type"] == "converged"


def test_edit_bodies_with_newlines_and_quotes():
    """The edit section logs repr(old)/repr(new): multi-line anchors and
    embedded quotes must replay exactly."""
    deriver = TraceDeriver()
    code = 'KEY = "us__q__p__1"\nOLD = "it\'s"\n'
    deriver.feed(section("tool call: write", json.dumps({"content": code})) +
                 section("write parser.py", code) +
                 section("tool result: write", "ok"))
    old = 'OLD = "it\'s"'
    new = 'OLD = "it\'s 2"\nEXTRA = "line"\n'
    events = deriver.feed(section("tool call: edit",
                                  json.dumps({"old": old, "new": new})) +
                          section("edit parser.py", f"- {old!r}\n+ {new!r}") +
                          section("tool result: edit", "replaced 1 occurrence"))
    snapshots = [e for e in events if e["type"] == "parser"]
    assert snapshots and snapshots[0]["content"] == code.replace(old, new, 1)
    # an anchor that is not in the snapshot: no event, a note instead
    stray = deriver.feed(section("tool call: edit",
                                 json.dumps({"old": "nope", "new": "x"})) +
                         section("edit parser.py", "- 'nope'\n+ 'x'"))
    assert "parser" not in types(stray)
