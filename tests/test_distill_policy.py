"""The harness's pure loop policy — advisories, crash-site extraction, and
summarizing compaction. The harness is loaded as a module (its import only
reads .env and imports statementledger; nothing touches the network)."""

import importlib.util
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

spec = importlib.util.spec_from_file_location(
    "distill_harness", TOOLS / "distill_harness.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


TRACEBACK = """Traceback (most recent call last):
  File "/logs/distill_x/round_7.py", line 30, in <module>
    statement = parse(pl.load_frames()[0])
  File "/logs/distill_x/round_7.py", line 23, in parse
    year = int(re.search(r"\\b(19|20)\\d{2}\\b", beside("Statement")).group())
ValueError: label not printed: Statement
"""


# --- prompt templates ----------------------------------------------------------


def test_prompt_templates_render_clean():
    assembled = harness.render_prompt("assembly.j2.md")
    for marker in ("# Frames", "# The deliverable", "# Rules",
                   "# Sandbox tools", "# Response contract", "KEY = ",
                   "pl.table", "if __name__"):
        assert marker in assembled, marker
    assert "{{" not in assembled and "{%" not in assembled
    task = harness.render_prompt("task.j2.md", name="stmt.pdf",
                                 pages=2, lines=41)
    assert "stmt.pdf" in task and "41 lines" in task
    assert "{{" not in task
    maintain = harness.render_prompt(
        "maintain_task.j2.md", key="us__x__y__1", names="a.pdf",
        name="a.pdf", pages=1, lines=9, verdict="verdict text",
        code="KEY = 'us__x__y__1'\n")
    assert 'KEY = "us__x__y__1"' in maintain and "verdict text" in maintain
    assert "{{" not in maintain
    addendum = harness.render_prompt("maintain_addendum.j2.md")
    assert "Maintenance mode" in addendum


# --- shown_indices -------------------------------------------------------------


def test_shown_indices_reads_show_render_and_dict_rows():
    stdout = (
        '[Page 0]\n'
        '038 | Date   Description      Amount\n'
        '003: Opening balance: $25,000.00\n'      # grep row: NOT an index view
        '012 ["15 Jan",77,113] ["1,122.33",282,329]\n'
        "0 {'a': 1}\n"
    )
    assert harness.shown_indices(stdout) == [38, 12, 0]


def test_shown_indices_rejects_midline_digits():
    assert harness.shown_indices("paid 500.00 on 03 Feb") == []


# --- crash_site ----------------------------------------------------------------


def test_crash_site_names_the_innermost_script_frame():
    assert harness.crash_site(TRACEBACK, "round_7.py") == "parse (round_7.py:23)"


def test_crash_site_falls_back_to_last_frame_without_traceback():
    assert harness.crash_site("no traceback here", "round_7.py") is None
    other = 'File "/usr/lib/x.py", line 9, in run\nRuntimeError\n'
    assert harness.crash_site(other, "round_7.py") == "run (x.py:9)"


# --- crash_streak_advisory -----------------------------------------------------


def test_crash_streak_advisory_fires_at_limit_and_multiples():
    limit = 3
    assert harness.crash_streak_advisory(2, limit, None) is None
    assert harness.crash_streak_advisory(4, limit, None) is None
    first = harness.crash_streak_advisory(3, limit, "parse (round_7.py:23)")
    assert "3 full scripts" in first and "round_7.py:23" in first
    again = harness.crash_streak_advisory(6, limit, None)
    assert "6 full scripts" in again and "round_7.py" not in again


# --- commit_nudge --------------------------------------------------------------


def test_commit_nudge_only_past_90_percent_or_budget():
    assert harness.commit_nudge(10, 30, 5, None) is None
    assert "full script" in harness.commit_nudge(27, 30, 5, None)
    assert harness.commit_nudge(5, 30, 2, None) is None
    assert "probe budget" in harness.commit_nudge(5, 30, 3, 3).lower()


# --- look_advisory -------------------------------------------------------------


def test_look_advisory_fires_once_until_coverage():
    note = harness.look_advisory(top_seen=False, tail_seen=False,
                                 already_shown=False)
    assert "TOP" in note and "END" in note
    tail_only = harness.look_advisory(True, False, False)
    assert "TOP" not in tail_only and "END" in tail_only
    assert harness.look_advisory(True, True, False) is None
    assert harness.look_advisory(False, False, True) is None


# --- compaction ----------------------------------------------------------------


def messages_of(size: int) -> list[dict]:
    """system + task + N alternating assistant/user rounds (scripts long
    enough to trip the fallback's >500-char elision)."""
    out = [{"role": "system", "content": "S"},
           {"role": "user", "content": "T"}]
    for i in range(size):
        out.append({"role": "assistant", "content": f"script {i} " + "x" * 600})
        out.append({"role": "user", "content": f"feedback {i} " + "y" * 400})
    return out


def test_compact_below_threshold_is_noop():
    messages = messages_of(4)
    before = [dict(m) for m in messages]
    assert harness.compact_messages(messages, threshold=10**9) == 0
    assert messages == before


def test_compact_summarizes_via_chat():
    messages = messages_of(8)
    suffix_before = [dict(m) for m in harness.split_keep(messages)[2]]
    calls = []

    def fake_chat(msgs):
        calls.append(msgs)
        return "DIGEST: labels found at lines 2-5; stop word 'End of activity'"

    saved = harness.compact_messages(messages, threshold=1000, chat=fake_chat)
    assert saved > 0 and len(calls) == 1
    summaries = [m for m in messages if "DIGEST:" in str(m.get("content"))]
    assert len(summaries) == 1 and summaries[0]["role"] == "user"
    # the verbatim tail is untouched
    assert harness.split_keep(messages)[2] == suffix_before
    # system + task survive
    assert messages[0]["content"] == "S" and messages[1]["content"] == "T"


def test_compact_falls_back_when_chat_dies():
    messages = messages_of(8)

    def dead_chat(msgs):
        raise RuntimeError("endpoint down")

    saved = harness.compact_messages(messages, threshold=1000, chat=dead_chat)
    assert saved > 0
    placeholders = [m for m in messages
                    if "earlier full script was here" in str(m.get("content"))]
    assert placeholders  # old elision semantics applied


def test_split_keep_partitions():
    messages = messages_of(6)
    prefix, middle, suffix = harness.split_keep(messages, keep_assistants=3)
    assert prefix[0]["role"] == "system" and len(prefix) == 2
    assert len(middle) > 0
    assistants = [m for m in suffix if m["role"] == "assistant"]
    assert len(assistants) == 3
    assert prefix + middle + suffix == messages
