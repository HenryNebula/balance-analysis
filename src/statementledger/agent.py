"""Tool-calling agent harness for statementledger registry parsers.

The model is a coding agent with seven tools: render/grep/table/python to
look at the statement by section and location, write/edit to build parser.py
one step at a time, and run to gate it on EVERY statement. A VERIFIED run is
the finish line — the harness saves the registry entry and exits. There is
no whole-script re-emission (parser.py is the state, edit fixes one spot),
no probe/script distinction (python IS the ad-hoc tool), and no submission
protocol (run verifies or names exactly what to fix).

Usage:
    statementledger-distill statement1.pdf [statement2.pdf ...]
    statementledger-distill --script my_parser.py stmt.pdf
        # canned: stage the file as parser.py, lint + gate once, no model
    statementledger-distill --key us__bank__prod__1 old.pdf new.pdf
        # MAINTAIN: parser.py starts from the registry source; old statements
        # are the regression set; KEY is pinned
    statementledger-distill --built-in --script my_parser.py stmt.pdf
        # PROMOTE: save into src/statementledger/registry/ instead of the
        # user overlay (git is the built-in changelog; no history.jsonl)
    # repo checkout without the console script on PATH:
    uv run python -m statementledger.agent statement1.pdf
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader, StrictUndefined
except ImportError as _jinja_error:  # the agent is an optional extra
    raise SystemExit(
        "statementledger's distill agent needs jinja2 for its prompts.\n"
        "Install it with:  pip install 'statementledger[agent]'"
    ) from _jinja_error

from statementledger.env import load_env

load_env()

import statementledger as pl
from statementledger.frames import frames_for
from statementledger.looks import look_event
from statementledger.pdfhealth import ScannedPdfError
from statementledger.registry import (REGISTRY, key_problem, module_path,
                                      parser_file, save_parser)

# The engine this process validated, pinned into every sandboxed child: the
# editable install can be re-pointed at a worktree by any concurrent uv sync,
# and a model-written statementledger.py in the workdir shadows the package
# (the script's directory leads sys.path). Both broke benchmark sweeps.
VALIDATED_SRC = str(Path(pl.__file__).resolve().parent.parent)

# LLM endpoint (STATEMENTLEDGER_LLM_* in .env / environment; see .env.example)
BASE_URL = os.environ.get("STATEMENTLEDGER_LLM_URL", "").rstrip("/")
MODEL = os.environ.get("STATEMENTLEDGER_LLM_MODEL", "")
TIMEOUT = int(os.environ.get("STATEMENTLEDGER_LLM_TIMEOUT", "300"))
MAX_TOKENS = 32_000

SCRIPT_TIMEOUT = 60  # wall clock per sandboxed child, seconds
OUTPUT_TAIL = 12_000  # chars of python-tool stdout fed back to the model
TOOL_RESULT_CAP = 6_000  # chars per look-tool result (render/table)

TURN_DEFAULT = int(os.environ.get("STATEMENTLEDGER_LLM_TURNS", "80"))
CRASH_STREAK = int(os.environ.get("STATEMENTLEDGER_CRASH_STREAK", "3"))
COMPACT_CHARS = int(os.environ.get("STATEMENTLEDGER_COMPACT_CHARS", "120000"))
LOOK_STREAK = int(os.environ.get("STATEMENTLEDGER_MAX_LOOK_STREAK", "6"))
CHAT_FAIL_STREAK = 3  # consecutive dead chat calls before giving up
REPAIR_STREAK = 5  # consecutive text-only replies before giving up

REPAIR_MESSAGE = (
    "Respond with exactly one tool call — no prose, no code fences. The "
    "tool schemas are attached to every request."
)

LOOP_MESSAGE = (
    "You are in a loop: the same call keeps being refused, and repeating "
    "it will not change the result. Step back — call read to see parser.py "
    "as it is, make one edit toward the last verdict's named error, then "
    "run. If your probe keeps crashing, read the LAST line of its "
    "traceback: it names the exact wrong assumption."
)

LOOP_REDIRECT_AT = 3  # consecutive refused turns before the redirect message
LOOP_ABORT_AT = 6     # ...before the run is abandoned with a diagnosis


# --- Prompts (jinja templates in tools/prompts/) --------------------------------

PROMPT_DIR = Path(__file__).parent / "prompts"
_prompt_env = Environment(
    loader=FileSystemLoader(PROMPT_DIR), autoescape=False,
    trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
    undefined=StrictUndefined,
)


def render_prompt(template: str, **context) -> str:
    """Render tools/prompts/<template>. StrictUndefined — a missing variable
    or template typo fails loudly instead of shipping a half-empty prompt."""
    return _prompt_env.get_template(template).render(**context)


AGENT_SYSTEM_PROMPT = render_prompt("agent_system.j2.md")


# --- LLM transport (OpenAI-compatible tool calling) ------------------------------


def chat_tools(messages: list[dict], tools: list | None = None,
               temperature: float = 0.0) -> dict:
    """Chat completion returning the assistant MESSAGE DICT (content and/or
    tool_calls). Same transport contract as the old chat_code: Bearer auth,
    greedy sampling (temperature 0 — the loop may pass a one-shot escape
    temperature after a duplicate call), retry with backoff on transient
    failures, no retry on 4xx (context overflow is not transient),
    llama-server chat_template_kwargs only to local endpoints, OpenRouter
    provider pin and reasoning flag when configured."""
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("STATEMENTLEDGER_LLM_API_KEY") or os.environ.get(
        "OPENROUTER_API_KEY"
    )
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body: dict = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": MAX_TOKENS,
    }
    if tools:
        body["tools"] = tools
    if re.match(r"https?://(127\.|localhost|\[::1\])", BASE_URL):
        body["chat_template_kwargs"] = {"enable_thinking": False}
    provider_pin = os.environ.get("STATEMENTLEDGER_LLM_PROVIDER", "").strip()
    if provider_pin:
        body["provider"] = {
            "only": [p.strip() for p in provider_pin.split(",") if p.strip()],
            "allow_fallbacks": False,
        }
    if os.environ.get("STATEMENTLEDGER_LLM_REASONING", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        body["reasoning"] = {"enabled": True}

    delays = (0, 5, 15, 45)
    last_error = "unknown"
    endpoint = BASE_URL.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        request = urllib.request.Request(
            endpoint, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                payload = json.load(response)
            message = payload.get("choices", [{}])[0].get("message", {})
            if (message.get("content") and message["content"].strip()) \
                    or message.get("tool_calls"):
                return message
            last_error = f"empty message: {json.dumps(payload)[:300]}"
        except urllib.error.HTTPError as error:
            last_error = f"HTTP {error.code}: {error.read()[:300]!r}"
            if error.code not in (429, 402) and error.code < 500:
                hint = (
                    " (context window overflow is the usual cause)"
                    if error.code in (400, 413)
                    else " (guardrail/data-policy endpoint exclusions show up as 404)"
                    if error.code == 404
                    else ""
                )
                raise RuntimeError(  # 4xx is not transient — do not retry
                    f"chat endpoint rejected the request: {last_error}{hint}"
                ) from error
        except urllib.error.URLError as error:
            last_error = f"transport: {error}"
        print(f"  chat attempt {attempt + 1} failed ({last_error[:120]}); retrying")
    raise RuntimeError(f"chat endpoint failed after {len(delays)} attempts: {last_error}")


def normalize_message(message: dict, turn: int) -> dict:
    """Defensive normalization of provider quirks: synthesized tool_call ids,
    dict-shaped arguments stringified, type defaulted. Returns the message
    as it will be appended to history (protocol-legal everywhere)."""
    calls = []
    for index, call in enumerate(message.get("tool_calls") or []):
        call = dict(call)
        call.setdefault("id", f"call_{turn}_{index}")
        call.setdefault("type", "function")
        function = dict(call.get("function") or {})
        if isinstance(function.get("arguments"), dict):
            function["arguments"] = json.dumps(function["arguments"])
        call["function"] = function
        calls.append(call)
    return {"role": "assistant",
            "content": message.get("content") or "",
            **({"tool_calls": calls} if calls else {})}


# --- Sandboxed execution ---------------------------------------------------------


def _child_limits() -> None:
    """Best-effort rlimits for the sandboxed child. Each is applied
    independently and skipped where the platform refuses it. An exception
    in preexec would kill subprocess.run with a detail-free SubprocessError,
    so nothing here may raise."""
    import resource

    for limit, value in (
        (resource.RLIMIT_CPU, (30, 30)),
        (resource.RLIMIT_FSIZE, (8 << 20, 8 << 20)),
        (resource.RLIMIT_AS, (1536 << 20, 1536 << 20)),
        (resource.RLIMIT_NOFILE, (64, 64)),
    ):
        try:
            resource.setrlimit(limit, value)
        except (ValueError, OSError):
            pass


def network_prefix() -> list[str]:
    """['unshare', '-rn', '--'] when unprivileged user namespaces can give
    the child a network-less namespace; [] (plain run) otherwise."""
    if not shutil.which("unshare"):
        return []
    try:
        probe = subprocess.run(
            ["unshare", "-rn", "--", "/bin/true"],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return ["unshare", "-rn", "--"] if probe.returncode == 0 else []


def run_script(workdir: Path, name: str) -> dict:
    """Run workdir/name.py in the sandbox; returns what the model sees."""
    script = workdir / f"{name}.py"
    command = [
        *network_prefix(),
        sys.executable,
        # -E -s: ignore PYTHON* env vars and user site-packages (the child
        # env is already sanitised); unlike -I this KEEPS the script's own
        # directory on sys.path so workdir-relative imports resolve.
        "-E",
        "-s",
        script.name,
    ]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=workdir,
            env={
                "HOME": str(workdir),
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C.UTF-8",
                "PYTHONIOENCODING": "utf-8",
            },
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT,
            preexec_fn=_child_limits,
        )
        outcome = {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "elapsed": time.perf_counter() - started,
        }
    except subprocess.TimeoutExpired:
        outcome = {
            "returncode": -1,
            "stdout": "",
            "stderr": f"script exceeded the {SCRIPT_TIMEOUT}s wall-clock limit",
            "elapsed": SCRIPT_TIMEOUT,
        }
    (workdir / f"{name}.out").write_text(
        f"returncode: {outcome['returncode']}\n--- stdout ---\n"
        f"{outcome['stdout']}\n--- stderr ---\n{outcome['stderr']}\n"
    )
    return outcome


GATE_RUNNER = '''import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, {validated_src!r})  # the engine the harness validated —
import statementledger as pl           # beats venv stomps AND workdir shadows

spec = importlib.util.spec_from_file_location("candidate", Path("parser.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
verdict = pl.evaluate(module.parse, pl.load_frames(),
                      key=getattr(module, "KEY", None),
                      expect_ledgers={expect_ledgers})
print("GATE:" + json.dumps(verdict))
'''


def py_gate(workdir: Path, trace: Trace, pins: dict[str, int] | None = None,
            turn: int = 0) -> dict:
    """The engine gate: import workdir/parser.py in a fresh sandbox and run
    parse() on EVERY statement. Verdict shape matches evaluate(); the extra
    'gate_crashed' key says the child itself died (syntax error, timeout).
    `turn` labels the gate event (0 = the pre-loop baseline/canned zone)."""
    (workdir / "gate_parser.py").write_text(
        GATE_RUNNER.format(expect_ledgers=repr(pins),
                           validated_src=VALIDATED_SRC))
    outcome = run_script(workdir, "gate_parser")
    verdict = None
    if outcome["returncode"] == 0:
        match = re.search(r"^GATE:(\{.*\})$", outcome["stdout"], re.M)
        if match:
            verdict = json.loads(match.group(1))
    if verdict is None:
        error = outcome["stderr"][-400:] or outcome["stdout"][-200:]
        verdict = {"key": None, "stage": "parse", "clean": 0, "verified": False,
                   "results": [{"name": "gate", "ok": False, "rows": 0,
                                "error": f"gate failed: {error}"}]}
    verdict["gate_crashed"] = outcome["returncode"] != 0
    if verdict.get("verified"):
        problem = key_problem(verdict.get("key") or "")
        if problem:
            verdict["verified"] = False
            verdict["results"].append({
                "name": "key", "ok": False, "rows": 0, "error": problem,
            })
    trace.log(
        "parser.py statementledger gate",
        f"exit {outcome['returncode']}\n{json.dumps(verdict, default=str)}",
    )
    event = {"turn": turn, "verdict": verdict}
    if turn == 0:
        event["baseline"] = True  # the pre-loop staged parser, not a loop edit
    trace.emit("gate", **event)
    return verdict


def py_feedback(evaluation: dict) -> str:
    """Per-statement verdict lines."""
    lines = []
    for result in evaluation["results"]:
        if result["ok"]:
            ledgers = result.get("ledgers", 1)
            suffix = f" transactions in {ledgers} ledgers" if ledgers > 1 \
                else " transactions"
            lines.append(f"  {result['name']}: OK — {result['rows']}{suffix}")
        else:
            lines.append(f"  {result['name']}: FAILED — {result['error']}")
    return ("statementledger gate (parse() re-run on EVERY statement; schema "
            "+ reconcile required):\n" + "\n".join(lines))


# --- Transcription guard ---------------------------------------------------------


MONEY_LITERAL_RE = re.compile(r"^\$?\d{1,3}(,\d{3})+(\.\d{2})?\s*(CR|DR)?$", re.I)


def lint_code(code: str) -> str | None:
    """Reject parsers that smuggle statement data in as literals, and broad
    except handlers that would swallow the errors the feedback loop depends
    on. Regex strings pass; range(...) bounds are exempt. Returns the
    violation text or None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        return f"script does not parse: {error}"
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and (
            node.type is None
            or (isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"))
        ):
            return (
                "try/except would swallow the error and hide it from you — "
                "let the parser crash; the gate feeds the exact traceback "
                "back"
            )
    range_args = {
        arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "range"
        for arg in node.args
        if isinstance(arg, ast.Constant)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
            continue
        value = node.value
        if isinstance(value, str) and MONEY_LITERAL_RE.match(value.strip()):
            return (
                f"string literal {value!r} looks like a printed amount — "
                "transcribing statement values into the parser is forbidden; "
                "derive values from the frames at runtime"
            )
        if (
            isinstance(value, (int, float))
            and abs(value) > 9999
            and node not in range_args
        ):
            return (
                f"numeric literal {value} is in the transaction-amount range — "
                "hardcoding data is forbidden; compute values from the frames"
            )
        if (
            isinstance(value, float) and abs(value) >= 50
            and value != int(value)
            and abs(value * 100 - round(value * 100)) < 1e-9
        ):
            return (
                f"float literal {value} looks like a printed amount (cents, "
                ">= 50) — transcribing statement values into the parser is "
                "forbidden; derive them from the frames at runtime"
            )
    return None


# --- Trace + event emission --------------------------------------------------------


def _utc_ts() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")


class Trace:
    """Append-only run logs next to the scripts and frames they describe.

    Two files, one truth: trace.log is the human-readable bracketed log;
    events.jsonl is the SAME run as structured events (the vocabulary
    web.agent_events documents — the web run manager tails this file
    directly instead of parsing trace.log). emit() is called at exactly the
    points where the legacy trace-derivation would produce an event, so
    both paths yield the same trajectory."""

    def __init__(self, directory: Path):
        self.path = directory / "trace.log"
        self._file = self.path.open("a", encoding="utf-8")
        self.events_path = directory / "events.jsonl"
        self._events = self.events_path.open("a", encoding="utf-8")
        self.terminal: dict | None = None  # the run's outcome, once

    def log(self, section: str, body: str = "") -> None:
        self._file.write(
            f"\n{'=' * 78}\n[{time.strftime('%H:%M:%S')}] {section}\n"
            f"{'-' * 78}\n{body}\n"
        )
        self._file.flush()

    def line(self, text: str) -> None:
        self._file.write(f"{text}\n")
        self._file.flush()

    def emit(self, kind: str, **fields) -> None:
        """One trajectory event, JSON per line, flushed (the web layer tails
        this file live). `kind` is the event type; fields ride as-is."""
        event = {"type": kind, "ts": _utc_ts(), **fields}
        self._events.write(json.dumps(event) + "\n")
        self._events.flush()

    def emit_terminal(self, kind: str, **fields) -> None:
        """The converged/failed outcome — exactly once (the first result
        wins; a later one never overrides it, same rule as the trace
        deriver's terminal state)."""
        if self.terminal is not None:
            return
        self.terminal = {"type": kind, **fields}
        self.emit(kind, **fields)

    def close(self) -> None:
        self._file.close()
        self._events.close()


# --- Loop policy (pure; unit-tested) ----------------------------------------------


def crash_site(stderr: str, script: str = "parser.py") -> str | None:
    """The innermost traceback frame inside parser.py, e.g.
    'parse (parser.py:23)' — the place a look tool should aim at."""
    frames = re.findall(r'File "(?:.*/)?([^"/]+)", line (\d+), in (\w+)', stderr)
    for name, line, fn in reversed(frames):
        if name == script:
            return f"{fn} ({name}:{line})"
    if frames:
        name, line, fn = frames[-1]
        return f"{fn} ({name}:{line})"
    return None


def crash_streak_advisory(streak: int, limit: int, site: str | None) -> str | None:
    if limit <= 0 or streak < limit or streak % limit:
        return None
    where = f", in `{site}`" if site else ""
    return (
        f"ADVISORY: {streak} runs in a row have crashed{where}. Editing "
        "blind will crash the same way — render the region that feeds that "
        "line, grep the label you keep guessing, read it, then edit."
    )


def look_streak_advisory(streak: int, limit: int) -> str | None:
    if limit <= 0 or streak < limit:
        return None
    return (
        f"ADVISORY: {streak} turns of looking without writing or running. "
        "You have the layout — write parser.py now (the table tool already "
        "solves the grid), then run it."
    )


def commit_nudge(turn: int, turns: int) -> str | None:
    if turn >= 0.9 * turns:
        return (
            "Advisory: 90% of the turns are used — only a VERIFIED run "
            "finishes. Run parser.py and fix exactly what the verdict names."
        )
    return None


def footer(ctx: "Ctx", turn: int, turns: int) -> str:
    gate = "not run"
    if ctx.last_gate is not None:
        gate = (f"{ctx.last_gate.get('clean', 0)}/{len(ctx.statements)} clean"
                + (" · VERIFIED" if ctx.last_gate.get("verified") else ""))
    return f"turn {turn}/{turns} · gate: {gate}"


# --- Context management ------------------------------------------------------------


def message_chars(messages: list[dict]) -> int:
    """Total payload chars — content AND tool-call arguments. A write's
    parser source and a python probe's code ride in the arguments; not
    counting them made compaction fire ~57% late in long local-model runs."""
    total = 0
    for message in messages:
        total += len(str(message.get("content", "")))
        for tool_call in message.get("tool_calls") or []:
            total += len(str(tool_call.get("function", {}).get("arguments", "")))
    return total


def _turn_groups(messages: list[dict]) -> list[list[dict]]:
    """Group history after the system+task prefix into model turns: an
    assistant message together with the tool messages answering its calls
    (they are elided as a unit — orphaned tool_call_ids are rejected by
    strict providers), or a plain message as its own group."""
    groups: list[list[dict]] = []
    for message in messages[2:]:
        if message["role"] == "assistant":
            groups.append([message])
        elif groups and message["role"] == "tool":
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def elide_middle(messages: list[dict], threshold: int = COMPACT_CHARS,
                 keep_pairs: int = 6) -> int:
    """Elide superseded model turns once the conversation crosses
    `threshold` chars; below it, nothing is touched. Kept verbatim: the
    system+task prefix and the last `keep_pairs` model turns. An elided
    turn with tool_calls keeps its assistant message as a stub ('turn
    elided — call the tool again') and drops its tool results, so the
    tool-call protocol stays legal. Mutates `messages` in place; returns
    chars saved."""
    if message_chars(messages) <= threshold:
        return 0
    before = message_chars(messages)
    groups = _turn_groups(messages)
    if len(groups) > keep_pairs:
        middle, kept = groups[:-keep_pairs], groups[-keep_pairs:]
    else:
        middle, kept = [], groups
    elided: list[dict] = []
    for group in middle:
        head = group[0]
        if head.get("role") == "assistant" and head.get("tool_calls"):
            names = ", ".join(
                call.get("function", {}).get("name", "?")
                for call in head["tool_calls"])
            elided.append({"role": "assistant",
                           "content": f"(turn elided: called {names} — "
                                      "call the tool again for fresh output)"})
        elif len(str(head.get("content", ""))) > 300:
            reduced = dict(head)
            reduced["content"] = str(head["content"])[:300] + "\n(elided)"
            elided.append(reduced)
        else:
            elided.append(head)
    messages[:] = messages[:2] + elided + [m for g in kept for m in g]
    return before - message_chars(messages)


OVERFLOW_RE = re.compile(
    r"exceeds the available context|context window overflow"
    r"|maximum context length|context_length_exceeded|prompt is too long",
    re.I)


def is_context_overflow(error_text: str) -> bool:
    """True when the endpoint rejected the request for SIZE (any provider
    phrasing seen so far; chat_tools' own 400/413 hint matches too). The
    cure is elision, not a retry."""
    return bool(OVERFLOW_RE.search(error_text))


# --- Tool catalog ------------------------------------------------------------------


def _schema(name: str, description: str, properties: dict,
            required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required},
    }}


TOOLS = [
    _schema("render", "Show a statement region as the page prints it — an "
             "x-preserving text wall with line indices (indices are what "
             "you pass as start).", {
                 "stmt": {"type": "integer", "description": "statement index (default 0)"},
                 "start": {"type": "integer", "description": "first line index"},
                 "n": {"type": "integer", "description": "line count (default 40, max 120)"},
                 "width": {"type": "integer", "description": "wall width in chars (default 110)"},
             }, ["start"]),
    _schema("grep", "Which lines hold a pattern — case-insensitive REGEX "
             "by default (like the grep CLI); \"regex\": false for a "
             "literal substring. The way to find labels and stop words "
             "before guessing them.", {
                 "stmt": {"type": "integer"},
                 "pattern": {"type": "string"},
                 "regex": {"type": "boolean", "description": "default true: case-insensitive regex; false: literal substring"},
                 "context": {"type": "integer", "description": "neighbor lines to include (max 3)"},
             }, ["pattern"]),
    _schema("table", "The ledger grid: (columns, rows) with wrapped "
             "descriptions merged, page furniture dropped, _line index per "
             "row. The geometry is solved — never rebuild it by hand.", {
                 "stmt": {"type": "integer"},
                 "start": {"type": "integer"},
                 "stop": {"type": "integer"},
                 "stop_terms": {"type": "array", "items": {"type": "string"},
                                "description": "first line containing any term ends the table"},
                 "header_index": {"type": "integer",
                                  "description": "line index of the header row (auto-detected when omitted)"},
             }, []),
    _schema("python", "Run ad-hoc code in a sandbox (no network, cwd holds "
             "frames.json; import statementledger as pl). For anything the "
             "typed tools don't cover.", {
                 "code": {"type": "string"},
             }, ["code"]),
    _schema("read", "Read parser.py with line numbers — the source of "
             "truth after edits, or after old turns were elided from the "
             "conversation.", {}, []),
    _schema("write", "Create or replace parser.py. Returns the lint "
             "verdict; run refuses a violating parser.", {
                 "content": {"type": "string", "description": "the complete parser.py source"},
             }, ["content"]),
    _schema("edit", "Fix one spot in parser.py. The old text must appear "
             "exactly once — include surrounding lines to make it unique.", {
                 "old": {"type": "string"},
                 "new": {"type": "string"},
             }, ["old", "new"]),
    _schema("run", "Lint parser.py and gate it on EVERY statement "
             "(schema + reconcile + totals). A VERIFIED result finishes "
             "the task; failures name the row and the fix.", {}, []),
]


@dataclass
class Ctx:
    workdir: Path
    statements: list[dict]
    trace: Trace
    require_key: str | None = None
    pins: dict[str, int] | None = None
    last_gate: dict | None = None
    last_gate_crashed: bool = False
    best: tuple[int, dict] | None = None
    probes: int = 0
    current_turn: int = 0  # 0 = pre-loop baseline/canned zone
    notes: dict[int, list[str]] = field(default_factory=dict)

    def note(self, turn: int, text: str) -> None:
        """A bare annotation that rides along with the turn's summary event
        (same fold the legacy trace-derivation performs)."""
        self.notes.setdefault(turn, []).append(text)


def _lines_for(ctx: Ctx, args: dict) -> tuple[list[dict] | None, str | None]:
    index = args.get("stmt", 0)
    if not isinstance(index, int) or not 0 <= index < len(ctx.statements):
        return None, (f"ERROR: stmt {index!r} out of range "
                      f"(0..{len(ctx.statements) - 1})")
    return ctx.statements[index]["lines"], None


def _tool_grep(ctx: Ctx, args: dict) -> str:
    lines, error = _lines_for(ctx, args)
    if error:
        return error
    pattern = str(args.get("pattern", ""))
    context = max(0, min(int(args.get("context", 0) or 0), 3))
    regex = args.get("regex", True)  # regex by default, like every grep
    if regex:
        try:
            matcher = re.compile(pattern, re.I).search
        except re.error as bad:
            return f"ERROR: invalid regex: {bad}"
    else:
        needle = pattern.lower()

        def matcher(text, needle=needle):
            return needle in text.lower()
    hits = [i for i, line in enumerate(lines)
            if matcher(pl.line_text(line))]
    shown: list[list] = []
    for hit in hits[:40]:
        for i in range(max(0, hit - context), min(len(lines), hit + context + 1)):
            if shown and shown[-1][0] == i:
                continue
            shown.append([i, pl.line_text(lines[i])[:160]])
    payload = {"hits": shown, "count": len(hits),
               "truncated": len(hits) > 40}
    result = json.dumps(payload, default=str)
    if not hits and re.search(r"[|*+?()\[\]{}^$\\]", pattern):
        mode = ("literal substring (regex: false)"
                if not regex else "regex")
        result += (f"\n0 matches in {mode} mode — the pattern contains "
                   "regex operators; "
                   + ("pass \"regex\": false for a literal substring search"
                      if regex else
                      "the default is regex mode (drop \"regex\": false)"))
    return result


def _tool_render(ctx: Ctx, args: dict) -> str:
    lines, error = _lines_for(ctx, args)
    if error:
        return error
    start = max(0, int(args.get("start", 0)))
    n = max(1, min(int(args.get("n", 40) or 40), 120))
    width = max(40, min(int(args.get("width", 110) or 110), 160))
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        pl.render(lines, n=n, start=start, width=width)
    wall = buffer.getvalue()
    return wall[:TOOL_RESULT_CAP] + ("\n(truncated)" if len(wall) > TOOL_RESULT_CAP else "")


def _tool_table(ctx: Ctx, args: dict) -> str:
    lines, error = _lines_for(ctx, args)
    if error:
        return error
    header = None
    if args.get("header_index") is not None:
        index = int(args["header_index"])
        if not 0 <= index < len(lines):
            return f"ERROR: header_index {index} out of range (0..{len(lines) - 1})"
        header = lines[index]
    stop_terms = tuple(args.get("stop_terms") or ())
    try:
        columns, rows = pl.table(
            lines, header=header,
            start=args.get("start"), stop=args.get("stop"),
            stop_terms=stop_terms)
    except ValueError as bad:
        return f"ERROR: {bad}"
    trimmed = [{k: (str(v)[:60] if v else v) for k, v in row.items()}
               for row in rows[:60]]
    payload = json.dumps({"columns": columns, "row_count": len(rows),
                          "rows_shown": len(trimmed), "rows": trimmed},
                         default=str)
    return payload[:TOOL_RESULT_CAP] + ("\n(truncated)" if len(payload) > TOOL_RESULT_CAP else "")


CRASH_HINTS = (
    (r"KeyError: 0|indices must be integers",
     "pl.load_frames() returns the LIST of statements — frames[i] is a "
     'statement, frames[i]["lines"] its lines, and an item is a '
     "[text, x1, x2] LIST, not a dict"),
    (r"'list' object has no attribute 'items'",
     "items are [text, x1, x2] lists, not dicts — index them; .items() is "
     "for dicts"),
)


def _crash_hint(stderr: str) -> str | None:
    for pattern, hint in CRASH_HINTS:
        if re.search(pattern, stderr):
            return hint
    return None


PROBE_BOOTSTRAP = '''import sys
sys.path.insert(0, {validated_src!r})  # the engine the harness validated
with open("probe_body.py") as _body:
    exec(compile(_body.read(), "probe_body.py", "exec"),
         {{"__name__": "__main__", "__file__": "probe_body.py"}})
'''


def _clear_shadow(ctx: Ctx) -> str | None:
    """A model-written statementledger.py in the workdir shadows the real
    package for every sandboxed child (the script's directory leads
    sys.path) — move it aside before it poisons the gates."""
    shadow = ctx.workdir / "statementledger.py"
    if not shadow.exists():
        return None
    shadow.replace(ctx.workdir / "statementledger.py.shadowed")
    ctx.trace.line("moved workdir statementledger.py shadow aside")
    return ("NOTE: a statementledger.py you wrote in this workdir was "
            "shadowing the real library for every sandboxed run (moved to "
            "statementledger.py.shadowed) — import statementledger, never "
            "recreate it")


def _tool_python(ctx: Ctx, args: dict) -> str:
    ctx.probes += 1
    note = _clear_shadow(ctx)
    (ctx.workdir / "probe_body.py").write_text(str(args.get("code", "")))
    (ctx.workdir / "probe.py").write_text(
        PROBE_BOOTSTRAP.format(validated_src=VALIDATED_SRC))
    outcome = run_script(ctx.workdir, "probe")
    parts = ([note] if note else [])
    if outcome["returncode"] != 0:
        parts.append(f"CRASHED:\n{outcome['stderr'][-1500:] or '(no stderr)'}")
        hint = _crash_hint(outcome["stderr"])
        if hint:
            parts.append(f"HINT: {hint}")
    if outcome["stdout"].strip():
        parts.append(outcome["stdout"][-OUTPUT_TAIL:])
    elif outcome["returncode"] == 0:
        parts.append("(no output — print() what you want to see)")
    return "\n\n".join(parts)


def _key_check(code: str, ctx: Ctx) -> str | None:
    """Maintain-mode KEY pin, checked before any sandbox work."""
    if not ctx.require_key:
        return None
    declared = re.search(r'^KEY\s*=\s*["\']([^"\']+)["\']', code, re.M)
    if not declared or declared.group(1) != ctx.require_key:
        return (f"MAINTAIN MODE: KEY must stay {ctx.require_key!r}"
                + (f", but parser.py declares {declared.group(1)!r}"
                   if declared else ", but parser.py declares none"))
    return None


def _tool_read(ctx: Ctx) -> str:
    path = ctx.workdir / "parser.py"
    if not path.exists():
        return "ERROR: parser.py does not exist yet — use write first"
    code = path.read_text()
    numbered = "\n".join(f"{i:03d} {line}"
                         for i, line in enumerate(code.splitlines(), 1))
    ctx.trace.log("read parser.py", f"{len(code)} chars")
    return numbered[:8000] + ("\n(truncated)" if len(numbered) > 8000 else "")


def _tool_write(ctx: Ctx, args: dict) -> str:
    content = str(args.get("content", ""))
    if len(content) > 24_000:
        return "ERROR: content over 24000 chars — parser.py should be under ~70 lines"
    (ctx.workdir / "parser.py").write_text(content)
    ctx.trace.log("write parser.py", content)
    ctx.trace.emit("parser", turn=ctx.current_turn, content=content)
    parts = [f"wrote parser.py ({len(content.splitlines())} lines)"]
    pin = _key_check(content, ctx)
    if pin:
        parts.append(f"ERROR: {pin}")
        return "\n".join(parts)
    violation = lint_code(content)
    parts.append(f"lint: {'VIOLATION — ' + violation if violation else 'OK'}")
    if not violation and "def parse(" not in content:
        parts.append("NOTE: no def parse(frames) yet — the deliverable is "
                     "KEY + parse()")
    return "\n".join(parts)


def _tool_edit(ctx: Ctx, args: dict) -> str:
    path = ctx.workdir / "parser.py"
    if not path.exists():
        return "ERROR: parser.py does not exist yet — use write first"
    code = path.read_text()
    old, new = str(args.get("old", "")), str(args.get("new", ""))
    if len(old) > 4000 or len(new) > 4000:
        return "ERROR: old/new over 4000 chars — edit a smaller spot"
    count = code.count(old)
    if count == 0:
        return ("ERROR: old text not found in parser.py — call read to see "
                "the file as it is, then retry with text copied exactly")
    if count > 1:
        return (f"ERROR: old text matches {count} places — include "
                "surrounding lines to make it unique")
    code = code.replace(old, new, 1)
    path.write_text(code)
    ctx.trace.log("edit parser.py", f"- {old!r}\n+ {new!r}")
    ctx.trace.emit("parser", turn=ctx.current_turn, content=code)
    parts = [f"replaced 1 occurrence"]
    pin = _key_check(code, ctx)
    if pin:
        parts.append(f"ERROR: {pin}")
        return "\n".join(parts)
    violation = lint_code(code)
    parts.append(f"lint: {'VIOLATION — ' + violation if violation else 'OK'}")
    return "\n".join(parts)


def _tool_run(ctx: Ctx) -> tuple[str, str]:
    path = ctx.workdir / "parser.py"
    if not path.exists():
        return "ERROR: parser.py does not exist yet — use write first", "run"
    code = path.read_text()
    pin = _key_check(code, ctx)
    if pin:
        return f"ERROR: {pin}", "run"
    violation = lint_code(code)
    if violation:
        return f"ERROR: lint refuses parser.py — {violation}", "run"
    note = _clear_shadow(ctx)
    verdict = py_gate(ctx.workdir, ctx.trace, ctx.pins, turn=ctx.current_turn)
    crashed = bool(verdict.pop("gate_crashed", False))
    ctx.last_gate = verdict
    ctx.last_gate_crashed = crashed
    clean = verdict.get("clean", 0)
    if clean > 0 and (ctx.best is None or clean > ctx.best[0]):
        ctx.best = (clean, {"key": verdict.get("key"), "code": code})
    shown = {k: v for k, v in verdict.items() if k != "results"}
    shown["results"] = [
        {**r, "error": (str(r.get("error", ""))[:300])}
        for r in verdict.get("results", [])]
    payload = json.dumps(shown, default=str)
    if len(payload) > 4000:
        payload = payload[:4000] + "\n(truncated)"
    if verdict.get("verified"):
        parts = [payload]
        if note:
            parts.append(note)
        parts.append("VERIFIED — reconciles every statement")
        return "\n".join(parts), "run"
    suffix = ""
    if crashed:
        site = crash_site((ctx.workdir / "gate_parser.out").read_text())
        suffix = f"\ngate crashed{f' in {site}' if site else ''} — the traceback is in the verdict errors"
    return payload + (("\n" + note) if note else "") + suffix, "run"


def _normalized_code(arguments: str) -> str | None:
    """python-tool code with comments stripped and whitespace collapsed —
    near-variant probes (same code, rotating '# check this' comments) are
    the same probe and should hit the duplicate ladder."""
    try:
        code = json.loads(arguments).get("code", "")
    except (ValueError, AttributeError):
        return None
    return re.sub(r"\s+", " ", re.sub(r"#[^\n]*", "", str(code))).strip()


def call_key(name: str, arguments: str, ctx: Ctx) -> str | None:
    """Canonical (tool, args) identity for duplicate detection; None when
    the arguments don't parse (those get their own error feedback). run
    and read take no arguments but consume parser.py — they key on the
    file's current content, so re-running after an edit is never a
    duplicate and re-running without one always is. python keys on the
    code with comments/whitespace normalized."""
    if name == "python":
        normalized = _normalized_code(arguments)
        return None if normalized is None else f"python#{hash(normalized)}"
    try:
        args = json.dumps(json.loads(arguments), sort_keys=True)
    except (ValueError, json.JSONDecodeError, TypeError):
        return None
    if name in ("run", "read"):
        parser = ctx.workdir / "parser.py"
        version = hash(parser.read_text()) if parser.exists() else "none"
        return f"{name} parser#{version}"
    return f"{name} {args}"


def duplicate_note(name: str, first_turn: int) -> str:
    if name in ("run", "read"):
        return (
            f"NOTE: {name} already ran against this exact parser.py (turn "
            f"{first_turn}) — the verdict will not change until you edit "
            "parser.py."
        )
    return (
        f"NOTE: identical {name} call was already made (turn {first_turn}) "
        "— the result is the same. Vary the arguments or take a different "
        "action (write the parser, run it)."
    )


def refusal_note(name: str, occurrences: int) -> str:
    if name in ("run", "read"):
        return (
            f"ERROR: {name} has been called {occurrences} times against "
            "this exact parser.py. The verdict does not change without an "
            "edit — use edit (or write), then run again."
        )
    return (
        f"ERROR: this exact {name} call has already run {occurrences} times "
        "with this exact result (see above). Refusing to run it again — "
        "change the arguments or take a different action."
    )


def dispatch(name: str, arguments: str, ctx: Ctx) -> tuple[str, str]:
    """Execute one tool call; returns (result text, kind) where kind is
    look/code/run. Never raises — failures are ERROR: strings the model
    reads. The result is logged to the trace beside its call and emitted as
    a tool event (plus a look event when the call pointed at statement 0)."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except (ValueError, json.JSONDecodeError) as bad:
        return (f"ERROR: arguments are not valid JSON ({bad}) — resend the "
                "tool call"), "look"
    ctx.trace.log(f"tool call: {name}", json.dumps(args, default=str)[:2000])
    if name == "grep":
        result, kind = _tool_grep(ctx, args), "look"
    elif name == "render":
        result, kind = _tool_render(ctx, args), "look"
    elif name == "table":
        result, kind = _tool_table(ctx, args), "look"
    elif name == "python":
        result, kind = _tool_python(ctx, args), "look"
    elif name == "read":
        result, kind = _tool_read(ctx), "look"
    elif name == "write":
        result, kind = _tool_write(ctx, args), "code"
    elif name == "edit":
        result, kind = _tool_edit(ctx, args), "code"
    elif name == "run":
        result, kind = _tool_run(ctx)
    else:
        return f"ERROR: unknown tool {name!r} (schemas are attached)", "look"
    ctx.trace.log(f"tool result: {name}", result[:2000])
    turn = ctx.current_turn
    ctx.trace.emit("tool", turn=turn, name=name, args=args,
                   result=result[:4000])
    if args.get("stmt", 0) == 0 and ctx.statements:
        look = look_event(turn, name, args, result,
                          ctx.statements[0]["lines"])
        if look:
            del look["type"]  # emit() adds it back
            ctx.trace.emit("look", **look)
    return result, kind


# --- The loop ----------------------------------------------------------------------


def agent_loop(
    pdfs: list[Path],
    workdir: Path,
    turns: int,
    trace: Trace,
    canned_script: Path | None = None,
    frames: dict | None = None,
    system_prompt: str | None = None,
    task_message: str | None = None,
    require_key: str | None = None,
    pins: dict[str, int] | None = None,
    look_streak_limit: int = LOOK_STREAK,
    crash_streak_limit: int = CRASH_STREAK,
    compact_chars: int = COMPACT_CHARS,
    chat=chat_tools,
) -> tuple[dict | None, bool, list[dict]]:
    """Returns ({key, code} or None, verified?, turn history). frames=None
    extracts from pdfs; tests inject fixture frames instead. canned_script
    stages a parser and gates it once with no model."""
    if frames is None:
        try:
            frames = frames_for(pdfs, workdir)
        except ScannedPdfError as error:
            trace.log("result", f"FAILED ({error})")
            trace.emit_terminal("failed", reason=str(error)[:200])
            return None, False, [{"outcome": str(error)[:400]}]
    statements = frames["statements"]
    first = statements[0]
    trace.log("frames", f"{len(statements)} statement(s); {first['name']}: "
              f"{len(first['lines'])} lines")
    trace.emit("frames", statements=len(statements), name=first["name"],
               lines=len(first["lines"]))
    ctx = Ctx(workdir=workdir, statements=statements, trace=trace,
              require_key=require_key, pins=pins)

    if canned_script is not None:
        (workdir / "parser.py").write_text(canned_script.read_text())
        result, _kind = _tool_run(ctx)
        trace.log("canned run", result)
        if ctx.last_gate and ctx.last_gate.get("verified"):
            return {"key": ctx.last_gate["key"],
                    "code": (workdir / "parser.py").read_text()}, True, []
        return (ctx.best[1] if ctx.best else None), False, [{"outcome": result[:400]}]

    system = system_prompt or AGENT_SYSTEM_PROMPT
    task = task_message or render_prompt(
        "agent_task.j2.md",
        name=first["name"],
        pages=1 + max(line["page"] for line in first["lines"]),
        lines=len(first["lines"]),
        others="\n".join(f"  {i}: {s['name']}" for i, s in enumerate(statements))
        if len(statements) > 1 else "",
    )
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]
    history: list[dict] = []
    chat_fails = 0
    repair_streak = 0
    crash_streak = 0
    look_streak = 0
    # (call_key -> [first_turn, occurrences]) since the last elision: the
    # 2nd identical call gets a NOTE, the 3rd+ is refused outright — a
    # temperature-0 model otherwise replays the same call forever, and each
    # replayed turn reinforces the pattern in-context.
    seen_calls: dict[str, list[int]] = {}
    escape_requests = 0  # upcoming requests sampled at 0.4 (loop escape)
    refusal_streak = 0

    for turn in range(1, turns + 1):
        ctx.current_turn = turn  # sections during this turn label with it
        saved = elide_middle(messages, threshold=compact_chars)
        if saved > 0:
            trace.log(f"turn {turn} context elided", f"{saved} chars saved")
            trace.emit("elided", turn=turn, chars=saved, forced=False)
            seen_calls.clear()  # post-elision repeats re-fetch lost output
        request_chars = message_chars(messages)  # what this turn's call costs
        escape_temperature = 0.4 if escape_requests > 0 else 0.0
        try:
            raw = chat(messages, TOOLS, temperature=escape_temperature)
        except RuntimeError as error:
            if is_context_overflow(str(error)):
                # the endpoint is the ground truth the char proxy guesses
                # at — shed the oldest turns hard and retry this turn
                saved = elide_middle(messages, threshold=0, keep_pairs=3)
                if saved > 0:
                    trace.log(f"turn {turn} context overflow — forced elision",
                              f"{saved} chars saved; retrying")
                    trace.emit("elided", turn=turn, chars=saved, forced=True)
                    seen_calls.clear()  # elided turns may be needed again
                    continue
            chat_fails += 1
            trace.log(f"turn {turn} chat failed", str(error))
            trace.emit("chat_failed", turn=turn, error=str(error)[:200])
            history.append({"turn": turn, "outcome": f"chat failed: {str(error)[:120]}"})
            if chat_fails >= CHAT_FAIL_STREAK:
                break
            continue
        chat_fails = 0
        message = normalize_message(raw, turn)
        messages.append(message)
        escape_requests = max(0, escape_requests - 1)

        calls = message.get("tool_calls") or []
        if not calls:
            repair_streak += 1
            trace.log(f"turn {turn} no tool call", message["content"][:500])
            if repair_streak >= REPAIR_STREAK:
                break
            messages.append({"role": "user", "content": REPAIR_MESSAGE})
            continue
        repair_streak = 0

        kinds: list[str] = []
        last_tool: dict | None = None
        duplicated = False
        refused = False
        for call in calls:
            function = call.get("function", {})
            tool_name = function.get("name", "")
            arguments = function.get("arguments", "{}")
            key = call_key(tool_name, arguments, ctx)
            note = None
            result = None
            if key is not None:
                if key in seen_calls:
                    seen_calls[key][1] += 1
                    duplicated = True
                    if seen_calls[key][1] >= 3:  # circuit breaker
                        result = refusal_note(tool_name, seen_calls[key][1])
                        kind = "look"
                        refused = True
                        trace.line(f"turn {turn}: REFUSED duplicate "
                                   f"{tool_name} call (x{seen_calls[key][1]})")
                        ctx.note(turn, f"REFUSED duplicate {tool_name} call "
                                       f"(x{seen_calls[key][1]})")
                    else:
                        note = duplicate_note(tool_name, seen_calls[key][0])
                else:
                    seen_calls[key] = [turn, 1]
            if result is None:
                result, kind = dispatch(tool_name, arguments, ctx)
            if note:
                result += "\n\n" + note
                trace.line(f"turn {turn}: duplicate {tool_name} call "
                           f"(first at turn {seen_calls[key][0]})")
                ctx.note(turn, f"duplicate {tool_name} call "
                               f"(first at turn {seen_calls[key][0]})")
            kinds.append(kind)
            entry = {"role": "tool", "tool_call_id": call["id"], "content": result}
            messages.append(entry)
            last_tool = entry
            if kind == "run" and ctx.last_gate and ctx.last_gate.get("verified"):
                entry["content"] += f"\n\n{footer(ctx, turn, turns)}"
                trace.line(f"turn {turn}: VERIFIED via run")
                history.append({"turn": turn, "calls": [c.get("function", {}).get("name") for c in calls],
                                "outcome": "VERIFIED"})
                trace.emit("turn", turn=turn, summary="VERIFIED",
                           notes=ctx.notes.pop(turn, [])
                           + ["VERIFIED via run"])
                return {"key": ctx.last_gate.get("key"),
                        "code": (workdir / "parser.py").read_text()}, True, history
        # one stochastic request after any duplicate — the escape hatch for
        # deterministic (temperature 0) replay loops; back to greedy after.
        if duplicated:
            escape_requests = max(escape_requests, 1)
        if refused:
            refusal_streak += 1
            if refusal_streak >= LOOP_ABORT_AT:
                trace.line(f"turn {turn}: loop abort — {refusal_streak} "
                           "consecutive turns of refused duplicate calls")
                history.append({"turn": turn,
                                "calls": [c.get("function", {}).get("name") for c in calls],
                                "outcome": f"loop abort after {refusal_streak} "
                                           "refused turns"})
                trace.emit("turn", turn=turn, summary="loop abort",
                           notes=ctx.notes.pop(turn, [])
                           + [f"loop abort — {refusal_streak} "
                              "consecutive turns of refused duplicate calls"])
                break
            if refusal_streak == LOOP_REDIRECT_AT:
                messages.append({"role": "user", "content": LOOP_MESSAGE})
                escape_requests = 3
                trace.line(f"turn {turn}: loop breaker — redirect message "
                           "sent, 3 sampled requests armed")
                ctx.note(turn, "loop breaker — redirect message sent, "
                               "3 sampled requests armed")
        else:
            refusal_streak = 0

        ran = "run" in kinds
        gate_crashed = ran and ctx.last_gate_crashed
        crash_streak = crash_streak + 1 if gate_crashed else 0
        look_streak = look_streak + 1 if set(kinds) <= {"look"} else 0

        site = crash_site((workdir / "gate_parser.out").read_text()) \
            if gate_crashed else None
        advisory = (crash_streak_advisory(crash_streak, crash_streak_limit, site)
                    or look_streak_advisory(look_streak, look_streak_limit)
                    or commit_nudge(turn, turns))
        if last_tool is not None:
            parts = [last_tool["content"]]
            if advisory:
                parts.append(advisory)
            parts.append(footer(ctx, turn, turns))
            last_tool["content"] = "\n\n".join(parts)

        summary = (f"{ctx.last_gate.get('clean', 0)}/{len(statements)} clean"
                   if ran and ctx.last_gate else
                   "lint fail" if ran else "+".join(dict.fromkeys(kinds)))
        trace.line(f"turn {turn}: {summary} · {request_chars // 1000}k chars")
        history.append({"turn": turn,
                        "calls": [c.get("function", {}).get("name") for c in calls],
                        "outcome": summary})
        trace.emit("turn", turn=turn, summary=summary,
                   chars_k=request_chars // 1000,
                   notes=ctx.notes.pop(turn, []))

    return (ctx.best[1] if ctx.best else None), False, history


# --- CLI -----------------------------------------------------------------------------


def _save_built_in(key: str, code: str) -> Path:
    """Atomic save into the curated package registry — the one write path
    into source, always an explicit --built-in decision; git is its
    changelog (no history.jsonl for built-ins). Warn loudly when an overlay
    copy shadows the key: promotion is a silent no-op until it is deleted."""
    out_path = REGISTRY / f"{key}.py"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(code)
    os.replace(tmp, out_path)
    shadow = parser_file(key)
    if shadow is not None and shadow != out_path:
        print(f"WARNING: {shadow} still shadows the built-in {out_path} — "
              "delete the overlay copy or every load keeps using it")
    return out_path


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("pdfs", nargs="+", type=Path, help="statements from one bank")
    cli.add_argument("--turns", type=int, default=TURN_DEFAULT,
                     help="agent turns (default: %(default)s)")
    cli.add_argument("--no-save", action="store_true", help="never write the registry")
    cli.add_argument("--built-in", action="store_true",
                     help="save into src/statementledger/registry/ (curated "
                          "source — git is its changelog) instead of the "
                          "user overlay; no revision history is recorded. "
                          "--no-save still wins when both are given")
    cli.add_argument("--key", default=None,
                     help="MAINTAIN the registered parser with this key — "
                          "parser.py starts from the registry source; the gate "
                          "requires ALL given statements to verify (pass old "
                          "AND new ones: the old are the regression set)")
    cli.add_argument("--crash-streak", type=int, default=CRASH_STREAK,
                     help="advisory after this many consecutive crashed runs "
                          "(default: %(default)s)")
    cli.add_argument("--compact-chars", type=int, default=COMPACT_CHARS,
                     help="elide old model turns past this many conversation "
                          "chars (default: %(default)s)")
    cli.add_argument("--max-look-streak", type=int, default=LOOK_STREAK,
                     help="advisory after this many look-only turns in a row "
                          "(default: %(default)s)")
    cli.add_argument("--script", type=Path, default=None,
                     help="stage this file as parser.py and gate once, no model "
                          "(canned offline test)")
    args = cli.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    workdir = Path("logs") / f"distill_{stamp}_{args.pdfs[0].stem[:30]}"
    workdir.mkdir(parents=True, exist_ok=True)
    trace = Trace(workdir)

    mode = f"canned script {args.script}" if args.script else f"{MODEL} at {BASE_URL}"
    print(f"distill agent: {len(args.pdfs)} statement(s), {mode}, {args.turns} turns")
    print(f"workdir: {workdir}")
    trace.log("run start", f"mode={mode}\npdfs={[str(p) for p in args.pdfs]}")
    trace.emit("run_started",
               model="canned script" if args.script else MODEL)

    system = AGENT_SYSTEM_PROMPT
    task_message = None
    require_key = None
    baseline = None
    pins = None
    if args.key:
        # maintain evolves the parser WHEREVER it lives: the user overlay
        # shadows the built-in (runtime-distilled parsers are user data)
        problem = key_problem(args.key)
        source_path = None if problem else parser_file(args.key)
        if source_path is None:
            print(f"no registered parser {args.key!r} "
                  f"({problem or f'looked in the overlay and {REGISTRY}'})")
            return 1
        baseline = source_path.read_text()
        (workdir / "parser.py").write_text(baseline)
        try:
            frames = frames_for(args.pdfs, workdir)
        except ScannedPdfError as error:
            print(f"error: {error}")
            trace.log("result", f"FAILED ({error})")
            trace.emit_terminal("failed", reason=str(error)[:200])
            trace.close()
            return 1
        verdict = py_gate(workdir, trace)
        verdict.pop("gate_crashed", None)
        print("baseline: " + ("VERIFIED" if verdict["verified"] else
                              f"{verdict['clean']}/{len(args.pdfs)} statements reconcile"))
        if verdict["verified"]:
            trace.log("result", "baseline already verifies every statement")
            trace.emit_terminal("converged", key=None, saved=False)
            print(f"{args.key} already verifies all {len(args.pdfs)} "
                  "statement(s) — nothing to maintain.")
            trace.close()
            return 0
        pins = {result["name"]: result["ledgers"] for result in verdict["results"]
                if result["ok"]}
        if pins:
            trace.log("coverage pins", json.dumps(pins))
            print(f"coverage pinned from baseline: {pins}")
        first = frames["statements"][0]
        task_message = render_prompt(
            "agent_task.j2.md",
            name=first["name"],
            pages=1 + max(line["page"] for line in first["lines"]),
            lines=len(first["lines"]),
            others="\n".join(f"  {i}: {s['name']}" for i, s in enumerate(frames["statements"]))
            if len(frames["statements"]) > 1 else "",
        ) + ("\n\nBaseline verdict (the CURRENT parser.py, run on every "
             "statement):\n" + py_feedback(verdict))
        system += "\n\n" + render_prompt("agent_maintain_addendum.j2.md")
        require_key = args.key

    artifact, verified, history = agent_loop(
        args.pdfs, workdir, args.turns, trace, canned_script=args.script,
        system_prompt=system, task_message=task_message, require_key=require_key,
        pins=pins, look_streak_limit=args.max_look_streak,
        crash_streak_limit=args.crash_streak, compact_chars=args.compact_chars,
    )
    if artifact is None or not verified:
        reason = (f"FAILED ({'no artifact' if artifact is None else 'not verified'})")
        trace.log("result", reason)
        trace.emit_terminal("failed", reason=reason[:200])
        trace.log("turn history", json.dumps(history, indent=2))
        trace.close()
        print("\nFAILED: no verified parser.")
        print(json.dumps(history, indent=2))
        return 1

    key = artifact["key"]
    trace.log("verified parser", artifact["code"])
    if baseline is not None:
        import difflib

        diff = [
            line for line in difflib.unified_diff(
                baseline.splitlines(), artifact["code"].splitlines(),
                lineterm="", n=0)
            if line[:1] in "+-" and line[:3] not in ("+++", "---")
        ]
        changelog = ("git history is the parser's changelog"
                     if args.built_in else
                     "the overlay's history.jsonl is the parser's changelog "
                     "(history.jsonl beside the parser; tools/"
                     "registry_history.py in the repo)")
        print(f"maintained in place: {len(diff)} lines changed ({changelog})")
        trace.log("maintenance diff", "\n".join(diff[:80]))
    if args.no_save:
        print(f"\nVERIFIED (not saved, --no-save); parser drafted in {workdir}")
        trace.log("result", f"VERIFIED (not saved); parser drafted in {workdir}")
        trace.emit_terminal("converged", key=artifact["key"], saved=False)
        import importlib.util

        draft = workdir / "parser.py"
        spec = importlib.util.spec_from_file_location("verified_draft", draft)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        frames = frames_for(args.pdfs, workdir)["statements"]
        for pdf, stmt_frames in zip(args.pdfs, frames):
            for ledger in pl.ledgers_of(module.parse(stmt_frames)):
                line = (
                    f"  {pdf.name} [{ledger['account_number']}]: "
                    f"{len(ledger['transactions'])} transactions, "
                    f"opening {ledger['opening_balance']:,.2f} -> "
                    f"closing {ledger['closing_balance']:,.2f}"
                )
                print(line)
                trace.line(line)
        trace.close()
        return 0

    # every statement the gate verified against, pinned into the history
    # record (sha16 — the same id the web store gives an uploaded PDF)
    verified_against = tuple(
        hashlib.sha256(pdf.read_bytes()).hexdigest()[:16] for pdf in args.pdfs)
    if args.built_in:
        out_path = _save_built_in(key, artifact["code"])
        print(f"\nVERIFIED parser saved (built-in): {out_path}")
    else:
        sha16 = save_parser(
            key, artifact["code"],
            source="maintain" if args.key else "distill",
            verified_against=verified_against,
            run_id=os.environ.get("STMTLEDGER_RUN_ID"))
        out_path = module_path(key)  # where it just landed, for the prints
        print(f"\nVERIFIED parser saved: {out_path} (revision {sha16})")
    print("Reuse with zero LLM:")
    print(f"  uv run statementledger statement.pdf --key {key}")
    trace.log("result", f"VERIFIED parser saved: {out_path}")
    trace.emit_terminal("converged", key=out_path.stem, saved=True)

    for pdf in args.pdfs:
        for ledger in pl.parse_all(str(pdf), key=key):
            line = (
                f"  {pdf.name} [{ledger['account_number']}]: "
                f"{len(ledger['transactions'])} transactions, "
                f"opening {ledger['opening_balance']:,.2f} -> "
                f"closing {ledger['closing_balance']:,.2f}"
            )
            print(line)
            trace.line(line)
    trace.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
