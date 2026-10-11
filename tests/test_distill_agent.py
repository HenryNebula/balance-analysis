"""The tool-calling agent harness — dispatch, loop policy, protocol-safe
elision, and a scripted end-to-end run through the real gate (no network)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

spec = importlib.util.spec_from_file_location(
    "distill_agent", TOOLS_DIR / "distill_agent.py")
agent = importlib.util.module_from_spec(spec)
sys.modules["distill_agent"] = agent
spec.loader.exec_module(agent)

FIXTURES = Path(__file__).parent / "fixtures"
PARSER = (Path(__file__).parent.parent
          / "src/statementledger/registry/us__copperkettle__checking__1.py")


def copper_frames() -> dict:
    return json.loads((FIXTURES / "copperkettle_checking.frames.json").read_text())


def make_ctx(tmp_path: Path, frames: dict | None = None) -> agent.Ctx:
    frames = frames or copper_frames()
    (tmp_path / "frames.json").write_text(json.dumps(frames))
    return agent.Ctx(workdir=tmp_path, statements=frames["statements"],
                     trace=agent.Trace(tmp_path))


def call(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name,
                                         "arguments": json.dumps(args)}}]}


def tool_result(message: dict) -> str:
    return message["content"]


# --- dispatch ------------------------------------------------------------------


def test_grep_tool_returns_hits_json(tmp_path):
    ctx = make_ctx(tmp_path)
    result = agent._tool_grep(ctx, {"pattern": "stop"})
    payload = json.loads(result)
    assert payload["count"] >= 1
    assert any("Transactions stop" in text for _i, text in payload["hits"])


def test_grep_is_regex_by_default_with_zero_hit_hint(tmp_path):
    ctx = make_ctx(tmp_path)
    # alternation works with no regex flag — the grep CLI convention
    result = agent._tool_grep(ctx, {"pattern": "stop|balance"})
    assert json.loads(result)["count"] >= 2
    # substring opt-out, zero hits, metachars present -> the mode hint
    literal = agent._tool_grep(ctx, {"pattern": "stop|balance",
                                     "regex": False})
    assert '"count": 0' in literal
    assert 'drop "regex": false' in literal
    # zero hits in regex mode with metachars -> the substring hint
    empty = agent._tool_grep(ctx, {"pattern": "nothere|ornothere"})
    assert 'regex": false' in empty


def test_duplicate_call_gets_a_note_and_results_are_logged(tmp_path):
    ctx_frames = copper_frames()
    (tmp_path / "frames.json").write_text(json.dumps(ctx_frames))
    script = [
        call("grep", {"pattern": "stop"}, "c1"),
        call("grep", {"pattern": "stop"}, "c2"),   # identical repeat
        call("write", {"content": PARSER.read_text()}, "c3"),
        call("run", {}, "c4"),
    ]
    state = {"i": 0}
    seen_tools: list[str] = []
    temperatures: list[float] = []

    def fake_chat(messages, tools, temperature=0.0):
        seen_tools.extend(m["content"] for m in messages if m["role"] == "tool")
        temperatures.append(temperature)
        reply = script[state["i"]]
        state["i"] += 1
        return reply

    trace = agent.Trace(tmp_path)
    artifact, verified, _ = agent.agent_loop(
        [Path("fixture.pdf")], tmp_path, 10, trace, frames=ctx_frames,
        chat=fake_chat)
    trace.close()
    assert verified
    assert any("identical grep call was already made (turn 1)" in c
               for c in seen_tools)
    # the duplicate turn (call 2, executed mid-turn) armed the one-shot
    # escape for the NEXT request (call 3), then greedy resumed
    assert temperatures == [0.0, 0.0, 0.4, 0.0]
    log = (tmp_path / "trace.log").read_text()
    assert "tool result: grep" in log and "tool result: run" in log


def test_run_after_edit_is_never_a_duplicate(tmp_path):
    ctx_frames = copper_frames()
    (tmp_path / "frames.json").write_text(json.dumps(ctx_frames))
    broken = PARSER.read_text().replace('"Transactions stop here."',
                                        '"Never printed anywhere"')
    script = [
        call("write", {"content": broken}, "c1"),
        call("run", {}, "c2"),                 # verdict: 0/1 — not verified
        call("edit", {"old": '"Never printed anywhere"',
                      "new": '"Transactions stop here."'}, "c3"),
        call("run", {}, "c4"),                 # NEW parser content: fresh run
    ]
    state = {"i": 0}
    seen_tools: list[str] = []

    def fake_chat(messages, tools, temperature=0.0):
        seen_tools.extend(m["content"] for m in messages if m["role"] == "tool")
        reply = script[state["i"]]
        state["i"] += 1
        return reply

    trace = agent.Trace(tmp_path)
    artifact, verified, _ = agent.agent_loop(
        [Path("fixture.pdf")], tmp_path, 10, trace, frames=ctx_frames,
        chat=fake_chat)
    trace.close()
    assert verified  # the post-edit run re-gated and finished the task
    assert not any("identical run call" in c or "already ran against" in c
                   for c in seen_tools)


def test_run_same_parser_refused_with_verdict_wording(tmp_path):
    ctx_frames = copper_frames()
    (tmp_path / "frames.json").write_text(json.dumps(ctx_frames))
    broken = PARSER.read_text().replace('"Transactions stop here."',
                                        '"Never printed anywhere"')
    script = [
        call("write", {"content": broken}, "c1"),
        call("run", {}, "c2"),                 # occurrence 1: executes
        call("run", {}, "c3"),                 # occurrence 2: NOTE
        call("run", {}, "c4"),                 # occurrence 3: REFUSED
        call("edit", {"old": '"Never printed anywhere"',
                      "new": '"Transactions stop here."'}, "c5"),
        call("run", {}, "c6"),
    ]
    state = {"i": 0}
    seen_tools: list[str] = []

    def fake_chat(messages, tools, temperature=0.0):
        seen_tools.extend(m["content"] for m in messages if m["role"] == "tool")
        reply = script[state["i"]]
        state["i"] += 1
        return reply

    trace = agent.Trace(tmp_path)
    artifact, verified, _ = agent.agent_loop(
        [Path("fixture.pdf")], tmp_path, 10, trace, frames=ctx_frames,
        chat=fake_chat)
    trace.close()
    assert verified
    notes = [c for c in seen_tools if "verdict will not change until you edit" in c]
    refusals = [c for c in seen_tools if "does not change without an edit" in c]
    assert notes and refusals


def test_message_chars_counts_tool_call_arguments():
    messages = [
        {"role": "user", "content": "x" * 100},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "write",
                                      "arguments": "y" * 500}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "z" * 50},
    ]
    assert agent.message_chars(messages) == 650


def test_is_context_overflow_matches_provider_phrasings():
    assert agent.is_context_overflow(
        "HTTP 400: request (65631 tokens) exceeds the available context")
    assert agent.is_context_overflow(
        "HTTP 400: ... (context window overflow is the usual cause)")
    assert agent.is_context_overflow(
        "HTTP 400: This model's maximum context length is 8192 tokens")
    assert not agent.is_context_overflow("HTTP 401: invalid api key")
    assert not agent.is_context_overflow("HTTP 404: unknown model")


def test_overflow_error_forces_elision_and_recovers(tmp_path):
    """A context-overflow 400 is a compaction signal, not a dead chat: the
    loop sheds the oldest turns hard (keep_pairs=3) and retries."""
    ctx_frames = copper_frames()
    (tmp_path / "frames.json").write_text(json.dumps(ctx_frames))
    broken = PARSER.read_text().replace('"Transactions stop here."',
                                        '"Never printed anywhere"')
    script = [
        call("grep", {"pattern": "stop"}, "c1"),
        call("render", {"start": 0, "n": 5}, "c2"),
        call("grep", {"pattern": "Account"}, "c3"),
        call("write", {"content": broken}, "c4"),
        call("run", {}, "c5"),                 # 0/1 clean — not verified
        call("edit", {"old": '"Never printed anywhere"',
                      "new": '"Transactions stop here."'}, "c6"),
        call("run", {}, "c7"),
    ]
    state = {"i": 0}
    request_chars: list[int] = []

    def fake_chat(messages, tools, temperature=0.0):
        request_chars.append(agent.message_chars(messages))
        if len(request_chars) == 5:            # history is big enough to elide
            raise RuntimeError(
                "chat endpoint rejected the request: HTTP 400: "
                "request (65631 tokens) exceeds the available context")
        reply = script[state["i"]]
        state["i"] += 1
        return reply

    trace = agent.Trace(tmp_path)
    artifact, verified, history = agent.agent_loop(
        [Path("fixture.pdf")], tmp_path, 20, trace, frames=ctx_frames,
        chat=fake_chat, compact_chars=10**9)  # scheduled elision never fires
    trace.close()
    assert verified
    assert not any("chat failed" in h.get("outcome", "") for h in history)
    log = (tmp_path / "trace.log").read_text()
    assert "forced elision" in log
    assert request_chars[5] < request_chars[4]  # retry ran on less history


def test_python_duplicate_key_ignores_comments_and_whitespace(tmp_path):
    ctx = make_ctx(tmp_path)
    first = agent.call_key("python", json.dumps(
        {"code": "a = 1  # probe v1\nprint(a)"}), ctx)
    second = agent.call_key("python", json.dumps(
        {"code": "a = 1 # probe v2\nprint(a)"}), ctx)
    assert first is not None and first == second  # rotating comments decoy
    other = agent.call_key("python", json.dumps(
        {"code": "a = 2  # probe v1\nprint(a)"}), ctx)
    assert other != first


def test_loop_breaker_redirects_then_aborts(tmp_path):
    """A model replaying one identical call forever: NOTE, then refusals —
    the redirect message lands at 3 refused turns (with 3 sampled requests
    armed), the run aborts with a diagnosis at 6 instead of burning the
    whole turn budget."""
    ctx_frames = copper_frames()
    (tmp_path / "frames.json").write_text(json.dumps(ctx_frames))
    state = {"i": 0}
    temperatures: list[float] = []
    user_messages: list[str] = []

    def chat(messages, tools, temperature=0.0):
        state["i"] += 1
        temperatures.append(temperature)
        user_messages.extend(m["content"] for m in messages
                             if m.get("role") == "user")
        return call("grep", {"pattern": "stop"}, f"c{state['i']}")

    trace = agent.Trace(tmp_path)
    artifact, verified, history = agent.agent_loop(
        [Path("fixture.pdf")], tmp_path, 30, trace, frames=ctx_frames,
        chat=chat)
    trace.close()
    assert not verified and artifact is None
    outcomes = [h["outcome"] for h in history]
    assert any(o.startswith("loop abort") for o in outcomes)
    assert history[-1]["turn"] < 30  # aborted early, not at the cap
    assert any("in a loop" in text for text in user_messages)
    # the NOTE at turn 2 arms a one-shot sample (turn 3); every refused turn
    # is also a duplicate, so the sample stays armed throughout the loop —
    # 0.4 from turn 3 on; redirect at streak 3 (turn 5), abort at streak 6
    # (turn 8)
    assert temperatures == [0.0, 0.0, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4]


def test_python_tool_hint_for_frames_shape_confusion(tmp_path):
    ctx = make_ctx(tmp_path)
    result = agent._tool_python(ctx, {"code": "import statementledger as pl\n"
                                         "frames = pl.load_frames()\n"
                                         "print(frames['statements'])"})
    assert "CRASHED" in result and "HINT:" in result
    assert "LIST of statements" in result


def test_lint_rejects_transcribed_cents_floats():
    bad = ('KEY = "us__a__b__1"\n'
           'def parse(frames):\n'
           '    return {"totals": [{"label": "T", "kind": "positive",'
           ' "value": 7269.81}]}\n')
    violation = agent.lint_code(bad)
    assert violation and "7269.81" in violation
    # whole floats and small constants stay legal
    assert agent.lint_code('KEY = "us__a__b__1"\n'
                           'def parse(f):\n    return {"x": 200.0 + 0.35}\n') is None


def test_clear_shadow_moves_model_written_statementledger(tmp_path):
    ctx = make_ctx(tmp_path)
    assert agent._clear_shadow(ctx) is None  # nothing to do
    (tmp_path / "statementledger.py").write_text("raise RuntimeError('x')\n")
    note = agent._clear_shadow(ctx)
    assert note and "shadowing the real library" in note
    assert not (tmp_path / "statementledger.py").exists()
    assert (tmp_path / "statementledger.py.shadowed").exists()
    assert agent._clear_shadow(ctx) is None  # second pass: already gone


def test_gate_bootstrap_beats_workdir_shadow(tmp_path):
    """Even with a poisoning statementledger.py still sitting in the workdir,
    the gate imports the engine the harness validated (sys.path.insert(0))
    and returns a real verdict, not the shadow's traceback."""
    ctx = make_ctx(tmp_path)
    (tmp_path / "parser.py").write_text(PARSER.read_text())
    (tmp_path / "statementledger.py").write_text("raise RuntimeError('shadow')\n")
    verdict = agent.py_gate(tmp_path, ctx.trace)
    assert verdict.get("verified"), verdict
    assert verdict["key"] == "us__copperkettle__checking__1"


def test_circuit_breaker_refuses_third_identical_call(tmp_path):
    ctx_frames = copper_frames()
    (tmp_path / "frames.json").write_text(json.dumps(ctx_frames))
    script = [
        call("grep", {"pattern": "stop"}, "c1"),
        call("grep", {"pattern": "stop"}, "c2"),   # repeat 1: NOTE
        call("grep", {"pattern": "stop"}, "c3"),   # repeat 2: REFUSED
        call("write", {"content": PARSER.read_text()}, "c4"),
        call("run", {}, "c5"),
    ]
    state = {"i": 0}
    seen_tools: list[str] = []

    def fake_chat(messages, tools, temperature=0.0):
        seen_tools.extend(m["content"] for m in messages if m["role"] == "tool")
        reply = script[state["i"]]
        state["i"] += 1
        return reply

    trace = agent.Trace(tmp_path)
    artifact, verified, _ = agent.agent_loop(
        [Path("fixture.pdf")], tmp_path, 10, trace, frames=ctx_frames,
        chat=fake_chat)
    trace.close()
    assert verified
    refused = [c for c in seen_tools if "Refusing to run it again" in c]
    assert refused and "has already run 3 times" in refused[0]
    log = (tmp_path / "trace.log").read_text()
    assert "REFUSED duplicate grep" in log


def test_render_tool_returns_wall(tmp_path):
    ctx = make_ctx(tmp_path)
    result = agent._tool_render(ctx, {"start": 0, "n": 5})
    assert "000 |" in result and "[Page 0]" in result
    assert len(result) <= agent.TOOL_RESULT_CAP + 20


def test_table_tool_grids_the_fixture(tmp_path):
    ctx = make_ctx(tmp_path)
    result = agent._tool_table(ctx, {"stop_terms": ["Transactions stop"]})
    payload = json.loads(result)
    assert payload["columns"] == ["Date", "Description", "Debit", "Credit",
                                  "Balance"]
    assert payload["row_count"] == 24  # wraps merged forward into their rows


def test_python_tool_runs_in_sandbox(tmp_path):
    ctx = make_ctx(tmp_path)
    result = agent._tool_python(ctx, {"code": "import statementledger as pl\n"
                                          "print(len(pl.load_frames()))"})
    assert "1" in result


def test_python_tool_reports_crash(tmp_path):
    ctx = make_ctx(tmp_path)
    result = agent._tool_python(ctx, {"code": "1/0"})
    assert "CRASHED" in result and "ZeroDivisionError" in result


def test_write_reports_lint_and_run_refuses_violations(tmp_path):
    ctx = make_ctx(tmp_path)
    bad = 'KEY = "us__x__y__1"\ndef parse(frames):\n    return {"x": "1,234.56"}\n'
    assert "lint: VIOLATION" in agent._tool_write(ctx, {"content": bad})
    result, _kind = agent._tool_run(ctx)
    assert result.startswith("ERROR: lint refuses")


def test_edit_requires_unique_anchor(tmp_path):
    ctx = make_ctx(tmp_path)
    agent._tool_write(ctx, {"content": "a = 1\nb = 2\na = 1\n"})
    missing = agent._tool_edit(ctx, {"old": "zzz", "new": "q"})
    assert missing.startswith("ERROR: old text not found")
    assert "call read" in missing
    ambiguous = agent._tool_edit(ctx, {"old": "a = 1", "new": "q"})
    assert "matches 2 places" in ambiguous
    ok = agent._tool_edit(ctx, {"old": "b = 2", "new": "b = 3"})
    assert ok.startswith("replaced 1 occurrence")
    assert (tmp_path / "parser.py").read_text() == "a = 1\nb = 3\na = 1\n"


def test_read_returns_numbered_parser_or_error(tmp_path):
    ctx = make_ctx(tmp_path)
    assert agent._tool_read(ctx).startswith("ERROR: parser.py does not exist")
    agent._tool_write(ctx, {"content": "KEY = 'us__x__y__1'\n"})
    numbered = agent._tool_read(ctx)
    assert numbered.startswith("001 KEY = 'us__x__y__1'")
    assert "002" not in numbered  # single line: no phantom numbering


def test_run_missing_parser(tmp_path):
    ctx = make_ctx(tmp_path)
    result, kind = agent._tool_run(ctx)
    assert result.startswith("ERROR: parser.py does not exist")
    assert kind == "run"


def test_dispatch_invalid_arguments_and_unknown_tool(tmp_path):
    ctx = make_ctx(tmp_path)
    result, _ = agent.dispatch("grep", "{not json", ctx)
    assert result.startswith("ERROR: arguments are not valid JSON")
    result, _ = agent.dispatch("teleport", "{}", ctx)
    assert result.startswith("ERROR: unknown tool")


def test_maintain_mode_pins_key(tmp_path):
    ctx = make_ctx(tmp_path, )
    ctx.require_key = "us__copperkettle__checking__1"
    renamed = PARSER.read_text().replace('KEY = "us__copperkettle__checking__1"',
                                         'KEY = "us__other__checking__1"')
    result = agent._tool_write(ctx, {"content": renamed})
    assert "KEY must stay" in result


# --- loop policy -----------------------------------------------------------------


def test_advisories():
    assert agent.crash_streak_advisory(2, 3, None) is None
    fired = agent.crash_streak_advisory(3, 3, "parse (parser.py:9)")
    assert "3 runs" in fired and "parser.py:9" in fired
    assert "6 runs" in agent.crash_streak_advisory(6, 3, None)
    assert agent.look_streak_advisory(5, 6) is None
    assert "write parser.py" in agent.look_streak_advisory(6, 6)
    assert agent.commit_nudge(70, 80) is None
    assert "VERIFIED run" in agent.commit_nudge(72, 80)


def test_footer_reports_gate_state(tmp_path):
    ctx = make_ctx(tmp_path)
    assert agent.footer(ctx, 3, 80) == "turn 3/80 · gate: not run"
    ctx.last_gate = {"clean": 1, "verified": False}
    assert agent.footer(ctx, 4, 80) == "turn 4/80 · gate: 1/1 clean"


def test_normalize_message_stringifies_dict_arguments():
    raw = {"content": "", "tool_calls": [
        {"function": {"name": "grep", "arguments": {"pattern": "x"}}}]}
    fixed = agent.normalize_message(raw, 7)
    the_call = fixed["tool_calls"][0]
    assert the_call["id"] == "call_7_0"
    assert json.loads(the_call["function"]["arguments"]) == {"pattern": "x"}


def test_elide_middle_keeps_tool_protocol_pairs():
    messages = [{"role": "system", "content": "S"},
                {"role": "user", "content": "T"}]
    for i in range(12):
        messages.append(call("grep", {"pattern": f"p{i}"}, f"c{i}"))
        messages.append({"role": "tool", "tool_call_id": f"c{i}",
                         "content": "hit " * 400})
    saved = agent.elide_middle(messages,
                               threshold=agent.message_chars(messages) - 200)
    assert saved > 0
    for index, message in enumerate(messages):
        if message.get("tool_calls"):
            following = messages[index + 1]
            assert following["role"] == "tool"
            assert following["tool_call_id"] == message["tool_calls"][0]["id"]
    assert messages[0]["content"] == "S" and messages[1]["content"] == "T"
    stubs = [m for m in messages if "turn elided" in str(m.get("content"))]
    assert stubs


# --- templates --------------------------------------------------------------------


def test_agent_templates_render_clean():
    system = agent.render_prompt("agent_system.j2.md")
    for marker in ("# Frames", "# The deliverable", "# Rules", "# Tools",
                   "# Workflow", "x=292", "KEY = ", "pl.table"):
        assert marker in system, marker
    assert "{{" not in system and "{%" not in system
    task = agent.render_prompt("agent_task.j2.md", name="s.pdf", pages=1,
                               lines=9, others="")
    assert "s.pdf" in task and "{{" not in task
    addendum = agent.render_prompt("agent_maintain_addendum.j2.md")
    assert "Maintenance mode" in addendum and "{{" not in addendum


# --- the loop (scripted chat, real gate) --------------------------------------------


def test_agent_loop_scripted_verified(tmp_path):
    ctx_frames = copper_frames()
    (tmp_path / "frames.json").write_text(json.dumps(ctx_frames))
    script = [
        call("grep", {"pattern": "Transactions stop"}, "c1"),
        call("write", {"content": PARSER.read_text()}, "c2"),
        call("run", {}, "c3"),
    ]
    state = {"i": 0}

    def fake_chat(messages, tools, temperature=0.0):
        reply = script[state["i"]]
        state["i"] += 1
        return reply

    trace = agent.Trace(tmp_path)
    artifact, verified, history = agent.agent_loop(
        [Path("fixture.pdf")], tmp_path, 10, trace, frames=ctx_frames,
        chat=fake_chat)
    trace.close()
    assert verified and artifact["key"] == "us__copperkettle__checking__1"
    assert (tmp_path / "parser.py").exists()
    assert history[-1]["outcome"] == "VERIFIED"


def test_agent_loop_continues_after_failed_run(tmp_path):
    ctx_frames = copper_frames()
    (tmp_path / "frames.json").write_text(json.dumps(ctx_frames))
    broken = PARSER.read_text().replace('"Transactions stop here."',
                                        '"Never printed anywhere"')
    script = [
        call("write", {"content": broken}, "c1"),
        call("run", {}, "c2"),
        call("write", {"content": PARSER.read_text()}, "c3"),
        call("run", {}, "c4"),
    ]
    state = {"i": 0}

    def fake_chat(messages, tools, temperature=0.0):
        reply = script[state["i"]]
        state["i"] += 1
        return reply

    trace = agent.Trace(tmp_path)
    artifact, verified, history = agent.agent_loop(
        [Path("fixture.pdf")], tmp_path, 10, trace, frames=ctx_frames,
        chat=fake_chat)
    trace.close()
    assert verified
    outcomes = [h["outcome"] for h in history]
    assert "0/1 clean" in outcomes and outcomes[-1] == "VERIFIED"


def test_agent_loop_repair_message_on_plain_text(tmp_path):
    ctx_frames = copper_frames()
    (tmp_path / "frames.json").write_text(json.dumps(ctx_frames))
    script = [{"content": "I will parse the statement now.", "tool_calls": []},
              call("grep", {"pattern": "stop"}, "c1")]
    state = {"i": 0}

    def fake_chat(messages, tools, temperature=0.0):
        seen.append(messages[-1])
        if state["i"] < len(script):
            reply = script[state["i"]]
            state["i"] += 1
            return reply
        return call("run", {}, "c99")  # never reached: turn cap below

    seen: list[dict] = []
    trace = agent.Trace(tmp_path)
    artifact, verified, _history = agent.agent_loop(
        [Path("fixture.pdf")], tmp_path, 2, trace, frames=ctx_frames,
        chat=fake_chat)
    trace.close()
    assert not verified
    repair = [m for m in seen if m.get("role") == "user"
              and "exactly one tool call" in m.get("content", "")]
    assert repair  # the plain-text reply got the repair message


def test_agent_loop_canned_script_gates_offline(tmp_path):
    ctx_frames = copper_frames()
    (tmp_path / "frames.json").write_text(json.dumps(ctx_frames))
    trace = agent.Trace(tmp_path)
    artifact, verified, _ = agent.agent_loop(
        [Path("fixture.pdf")], tmp_path, 1, trace,
        canned_script=PARSER, frames=ctx_frames)
    trace.close()
    assert verified and artifact["key"] == "us__copperkettle__checking__1"


# --- structured event emission (events.jsonl) -------------------------------------


def _events_file(tmp_path: Path) -> list[dict]:
    return [json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
            if line.strip()]


def test_emitted_events_match_the_derived_trace(tmp_path):
    """The agent emits its trajectory directly (events.jsonl); the web layer
    still derives the same vocabulary from trace.log for legacy workdirs.
    Both paths must agree — this is the drift guard."""
    from web.agent_events import TraceDeriver

    ctx_frames = copper_frames()
    (tmp_path / "frames.json").write_text(json.dumps(ctx_frames))
    script = [
        call("grep", {"pattern": "stop"}, "c1"),
        call("render", {"start": 0, "n": 20}, "c2"),
        call("write", {"content": PARSER.read_text()}, "c3"),
        call("run", {}, "c4"),                 # verifies -> terminal turn
    ]
    state = {"i": 0}

    def fake_chat(messages, tools, temperature=0.0):
        reply = script[state["i"]]
        state["i"] += 1
        return reply

    trace = agent.Trace(tmp_path)
    artifact, verified, _ = agent.agent_loop(
        [Path("fixture.pdf")], tmp_path, 10, trace, frames=ctx_frames,
        chat=fake_chat)
    trace.close()
    assert verified

    deriver = TraceDeriver(ctx_frames["statements"][0]["lines"])
    derived = deriver.feed((tmp_path / "trace.log").read_text())
    derived += deriver.close()
    emitted = _events_file(tmp_path)

    def strip(event):
        return {k: v for k, v in event.items() if k != "ts"}

    assert [e["type"] for e in emitted] == [e["type"] for e in derived]
    for live, legacy in zip(emitted, derived):
        live, legacy = strip(live), strip(legacy)
        if live["type"] == "tool":
            # legacy args/results are capped by the trace.log write (2000
            # chars) and its line-joined body drops one trailing newline;
            # emission carries them whole — compare within those limits
            assert live["name"] == legacy["name"]
            assert live["turn"] == legacy["turn"]
            capped = live["result"][:2000]
            if capped.endswith("\n"):
                capped = capped[:-1]
            assert capped == legacy["result"]
            assert legacy["args"] in ({}, live["args"])
            continue
        assert live == legacy, (live, legacy)


def test_canned_run_emits_baseline_gate_event(tmp_path):
    ctx_frames = copper_frames()
    (tmp_path / "frames.json").write_text(json.dumps(ctx_frames))
    trace = agent.Trace(tmp_path)
    agent.agent_loop([Path("fixture.pdf")], tmp_path, 1, trace,
                     canned_script=PARSER, frames=ctx_frames)
    trace.close()
    events = _events_file(tmp_path)
    gate = next(e for e in events if e["type"] == "gate")
    assert gate["turn"] == 0 and gate["baseline"] is True
    assert gate["verdict"]["verified"]


def test_scanned_pdf_fails_with_terminal_event(tmp_path, monkeypatch):
    def boom(pdfs, workdir):
        raise agent.ScannedPdfError("scan.pdf: the PDF has no text layer")

    monkeypatch.setattr(agent, "frames_for", boom)
    trace = agent.Trace(tmp_path)
    artifact, verified, history = agent.agent_loop(
        [Path("scan.pdf")], tmp_path, 5, trace)
    trace.close()
    assert artifact is None and not verified
    assert "no text layer" in history[0]["outcome"]
    events = _events_file(tmp_path)
    assert events[-1]["type"] == "failed"
    assert "no text layer" in events[-1]["reason"]
    # the trace keeps the human-readable twin of the terminal event
    assert "FAILED (scan.pdf" in (tmp_path / "trace.log").read_text()
