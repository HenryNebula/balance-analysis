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

    def fake_chat(messages, tools):
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

    def fake_chat(messages, tools):
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

    def fake_chat(messages, tools):
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
