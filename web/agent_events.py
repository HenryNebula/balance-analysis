"""Derive the web run-view trajectory from a distill agent's trace.log.

This is the LEGACY path: workdirs from agents before structured emission
(and any trace.log someone points the attach mode at). Current agents emit
the same event vocabulary directly into events.jsonl beside their trace.log
(statementledger.agent Trace.emit); the web run manager prefers that file
and falls back to this derivation — the vocabulary below is shared, so a
run view renders identically either way.

statementledger.agent is an OpenAI tool-calling loop whose run artifacts
(besides the evolving parser.py) are an append-only trace.log of bracketed
sections plus bare one-line notes, and the events.jsonl it derives from
them. This deriver tails trace.log and folds new bytes into the SAME SSE
event stream the run view consumes — so attached legacy runs and reloads of
finished runs all replay through one derivation.

Vocabulary (events.jsonl; `turn` replaces the REPL's round — 0 is the
baseline/canned zone before the loop starts):

    run_started {model}
    frames      {statements, name, lines}
    tool        {turn, name, args, result}     one per call+result pair
    gate        {turn, verdict}                py_gate's full verdict JSON
    parser      {turn, content}                parser.py snapshot after a
                                               write/edit (edit = anchor
                                               replace applied to the last
                                               snapshot — no file race)
    look        {turn, tool, label, page, rect, line_ids}
                                               what render/grep/table were
                                               LOOKING at, resolved against
                                               the stored statement's frames
    turn        {turn, summary, chars_k, notes}
    elided      {turn, chars, forced}
    chat_failed {turn, error}
    converged   {key, saved} / failed {reason}

Everything here is pure over the bytes handed in (TraceDeriver keeps the
cross-section state); tests feed recorded traces with no I/O.

Format note: Trace.log() writes a whole section in ONE flushed write, so a
body is complete when its bytes are; sections close when the next head (or
a bare `turn N:` note) arrives, and a torn tail line waits for more bytes.
"""

from __future__ import annotations

import ast
import datetime
import json
import re

from statementledger.looks import json_object, look_event

RULE = "=" * 78
DASH = "-" * 78
HEAD_TIME = re.compile(r"^\[(\d\d:\d\d:\d\d)\] (.+)$")
TURN_LINE = re.compile(r"^turn (\d+): (.*)$")
SUMMARY_LINE = re.compile(r"^(.*?) · (\d+)k chars$")
EDIT_BODY = re.compile(r"^- ('.*'|\".*\")\n\+ ('.*'|\".*\")$", re.S)
KEY_DECL = re.compile(r'^KEY\s*=\s*["\']([^"\']+)["\']', re.M)


def _ts() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")


class TraceDeriver:
    """Feed append-only trace.log bytes; get trajectory events back.

    lines: the FIRST statement's frames lines (None when the PDF is
    unavailable) — render/grep/table line indices resolve against them
    into look events with overlay rects. Only look calls on statement 0
    resolve (the run view's PDF pane shows the first statement).
    """

    def __init__(self, lines: list[dict] | None = None):
        self.lines = lines or []
        self.model = ""
        self.parser_code = ""   # latest parser.py snapshot
        self.terminal: dict | None = None
        self._buf = ""          # torn tail line (no newline yet)
        self._name: str | None = None  # open section's name, else None
        self._body: list[str] = []
        self._turn = 0          # last turn with a summary/terminal line
        self._saw_tool = False  # a gate before any tool call = baseline
        self._notes: dict[int, list[str]] = {}

    def key_hint(self) -> str | None:
        """The parser's KEY = declaration from the latest snapshot."""
        match = KEY_DECL.search(self.parser_code)
        return match.group(1) if match else None

    # -- the fold ------------------------------------------------------------

    def feed(self, text: str) -> list[dict]:
        """Consume new bytes; return the events they completed."""
        events: list[dict] = []
        self._buf += text
        *lines, self._buf = self._buf.split("\n")
        for line in lines:
            if self._name is not None and line == RULE:
                self._flush(events)
            elif line == RULE:
                pass  # section head closes nothing; time line comes next
            elif self._name is None and line == DASH:
                pass  # stray; ignore
            elif self._name is not None and self._head_pending:
                if line == DASH:
                    self._head_pending = False  # head complete: body starts
                else:  # not a head after all — a rule line inside a body
                    self._head_pending = False
                    self._body += [RULE, line]
            elif HEAD_TIME.match(line) and not self._name:
                match = HEAD_TIME.match(line)
                self._name, self._head_pending = match.group(2), True
            else:
                self._line(line, events)
        for event in events:
            event.setdefault("ts", _ts())
        return events

    _head_pending = False

    def _line(self, line: str, events: list[dict]) -> None:
        """A body line, or a bare note when no section is open."""
        match = TURN_LINE.match(line)
        if match:
            # a bare turn note closes the open section: bodies are atomic
            # writes, so this line arrived after the body ended
            if self._name is not None:
                self._flush(events)
            self._turn_line(int(match.group(1)), match.group(2), events)
            return
        if self._name is not None:
            self._body.append(line)
        # bare non-turn lines between sections (ledger summaries, etc.) are
        # not trajectory events; ignore

    def _flush(self, events: list[dict]) -> None:
        # the section's framing adds ONE newline after the body: drop that
        # empty line, keep the body's own trailing newline (parser.py
        # snapshots must survive byte-exact for edit replay)
        body_lines = list(self._body)
        if body_lines and body_lines[-1] == "":
            body_lines.pop()
        body = "\n".join(body_lines)
        name, self._name, self._body = self._name, None, []
        if name is None:
            return
        self._section(name, body, events)

    def close(self) -> list[dict]:
        """Flush the final open section (the trace file ended)."""
        events: list[dict] = []
        self._flush(events)
        for event in events:
            event.setdefault("ts", _ts())
        return events

    # -- sections ------------------------------------------------------------

    def _section(self, name: str, body: str, events: list[dict]) -> None:
        if name == "run start":
            mode = body.splitlines()[0] if body else ""
            match = re.match(r"mode=(.*?) at (\S+)$", mode)
            self.model = (match.group(1) if match
                          else mode.removeprefix("mode="))
            if self.model.startswith("canned script"):
                self.model = "canned script"  # drop the staged path
            events.append({"type": "run_started", "model": self.model})
        elif name == "frames":
            found = re.match(r"(\d+) statement\(s\); (.*?): (\d+) lines", body)
            if found:
                events.append({"type": "frames",
                               "statements": int(found.group(1)),
                               "name": found.group(2),
                               "lines": int(found.group(3))})
        elif name.startswith("tool call: "):
            tool = name.removeprefix("tool call: ")
            self._pending_call = (tool, json_object(body))
            self._saw_tool = True
        elif name.startswith("tool result: "):
            tool = name.removeprefix("tool result: ")
            call_tool, args = self._pending_call or (tool, {})
            self._pending_call = None
            if tool != call_tool:  # a call section got lost; still show it
                args = {}
            self._tool_event(self._pending_turn(), tool, args, body, events)
        elif name == "write parser.py":
            self.parser_code = body
            events.append({"type": "parser", "turn": self._pending_turn(),
                           "content": body})
        elif name == "edit parser.py":
            content = self._apply_edit(body)
            if content is not None:
                self.parser_code = content
                events.append({"type": "parser", "turn": self._pending_turn(),
                               "content": content})
            else:  # anchor not in the snapshot — it stays at the last one
                self._note(self._pending_turn(),
                           "edit could not be replayed for the code pane")
        elif name == "parser.py statementledger gate":
            verdict = _gate_verdict(body)
            if verdict is not None:
                turn = 0 if not self._saw_tool else self._pending_turn()
                gate = {"type": "gate", "turn": turn, "verdict": verdict}
                if turn == 0 and not self.parser_code:
                    gate["baseline"] = True  # maintain-mode staged parser
                events.append(gate)
        elif re.fullmatch(r"turn \d+ chat failed", name):
            turn = int(name.split()[1])
            self._note(turn, "chat failed")
            events.append({"type": "chat_failed", "turn": turn,
                           "error": body[:200]})
        elif re.fullmatch(r"turn \d+ no tool call", name):
            self._note(int(name.split()[1]),
                       "replied with text, no tool call")
        elif re.fullmatch(r"turn \d+ context elided", name):
            events.append(_elided(name, body, forced=False))
        elif re.fullmatch(r"turn \d+ context overflow — forced elision", name):
            events.append(_elided(name, body, forced=True))
        elif name == "coverage pins":
            self._note(0, f"coverage pinned from baseline: {body[:120]}")
        elif name == "canned run":
            self._note(0, "canned script gated once (no model)")
        elif name == "result":
            if self.terminal is None:  # peek may have emitted it already
                self._terminal(body, events)

    _pending_call: tuple[str, dict] | None = None

    def _tool_event(self, turn: int, tool: str, args: dict, body: str,
                    events: list[dict]) -> None:
        events.append({"type": "tool", "turn": turn, "name": tool,
                       "args": args, "result": body[:4000]})
        if args.get("stmt", 0) == 0 and self.lines:
            look = look_event(turn, tool, args, body, self.lines)
            if look:
                events.append(look)

    def _apply_edit(self, body: str) -> str | None:
        """The edit tool replaces a unique anchor; replay it on the last
        snapshot (deterministic — never races the live parser.py)."""
        match = EDIT_BODY.match(body)
        if not match:
            return None
        try:
            old = ast.literal_eval(match.group(1))
            new = ast.literal_eval(match.group(2))
        except (ValueError, SyntaxError):
            return None
        if old not in self.parser_code:
            return None
        return self.parser_code.replace(old, new, 1)

    def _terminal(self, body: str, events: list[dict]) -> None:
        first = body.splitlines()[0] if body else ""
        self.terminal = self._make_terminal(first)
        events.append(self.terminal)

    def _make_terminal(self, first: str) -> dict:
        if first.startswith("VERIFIED parser saved:"):
            key = re.search(r"([^/\\]+)\.py$", first)  # the file's stem
            return {"type": "converged",
                    "key": key.group(1) if key else self.key_hint(),
                    "saved": True}
        if first.startswith("VERIFIED (not saved"):
            return {"type": "converged", "key": self.key_hint(),
                    "saved": False}
        if first.startswith("baseline already verifies"):
            # maintain mode with nothing to do; the manager knows the key
            return {"type": "converged", "key": None, "saved": False}
        return {"type": "failed",
                "reason": first[:200] or "run finished"}

    def peek_terminal(self) -> list[dict]:
        """The open 'result' section's first line already decides the
        outcome — emit it before any closer arrives (an attached run has no
        process exit to flush the tail; idempotent with the later flush).
        Only peeks at a complete line: an empty _buf means the section's
        atomic write is fully visible, a torn tail waits for its bytes."""
        if (self.terminal is not None or self._name != "result"
                or self._buf or not self._body):
            return []
        self.terminal = self._make_terminal(self._body[0])
        return [{**self.terminal, "ts": _ts()}]

    # -- bare one-line notes ---------------------------------------------------

    def _turn_line(self, turn: int, rest: str, events: list[dict]) -> None:
        summary = SUMMARY_LINE.match(rest)
        if summary:
            events.append({"type": "turn", "turn": turn,
                           "summary": summary.group(1),
                           "chars_k": int(summary.group(2)),
                           "notes": self._notes.pop(turn, [])})
        elif rest.startswith("VERIFIED via run") or rest.startswith(
                "loop abort"):
            summary = ("VERIFIED" if rest.startswith("VERIFIED")
                       else "loop abort")
            events.append({"type": "turn", "turn": turn,
                           "summary": summary,
                           "notes": self._notes.pop(turn, []) + [rest]})
        else:  # duplicate / REFUSED / loop-breaker lines
            self._note(turn, rest[:160])
        self._turn = max(self._turn, turn)

    def _note(self, turn: int, text: str) -> None:
        self._notes.setdefault(turn, []).append(text)

    def _pending_turn(self) -> int:
        """Sections between two turn lines belong to the next turn."""
        return self._turn + 1


def _gate_verdict(body: str) -> dict | None:
    """The gate section is 'exit N' + the verdict JSON on later lines."""
    for line in body.splitlines():
        if line.startswith("{"):
            try:
                verdict = json.loads(line)
            except ValueError:
                continue
            if isinstance(verdict, dict):
                return verdict
    return None


def _elided(section: str, body: str, forced: bool) -> dict:
    turn = int(re.search(r"turn (\d+)", section).group(1))
    chars = re.search(r"(\d+) chars saved", body)
    return {"type": "elided", "turn": turn,
            "chars": int(chars.group(1)) if chars else 0, "forced": forced}
