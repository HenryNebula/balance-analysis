"""Tool-calling agent harness for statementledger registry parsers.

The model is a coding agent with seven tools: render/grep/table/python to
look at the statement by section and location, write/edit to build parser.py
one step at a time, and run to gate it on EVERY statement. A VERIFIED run is
the finish line — the harness saves the registry entry and exits. There is
no whole-script re-emission (parser.py is the state, edit fixes one spot),
no probe/script distinction (python IS the ad-hoc tool), and no submission
protocol (run verifies or names exactly what to fix).

Usage (from the repo root):
    uv run python tools/distill_agent.py statement1.pdf [statement2.pdf ...]
    uv run python tools/distill_agent.py --script my_parser.py stmt.pdf
        # canned: stage the file as parser.py, lint + gate once, no model
    uv run python tools/distill_agent.py --key us__bank__prod__1 old.pdf new.pdf
        # MAINTAIN: parser.py starts from the registry source; old statements
        # are the regression set; KEY is pinned
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from envloader import load_env

load_env()

import statementledger as pl
from statementledger.frames import frames_for
from statementledger.registry import REGISTRY, key_problem

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


def chat_tools(messages: list[dict], tools: list | None = None) -> dict:
    """Chat completion returning the assistant MESSAGE DICT (content and/or
    tool_calls). Same transport contract as the old chat_code: Bearer auth,
    temperature 0, retry with backoff on transient failures, no retry on
    4xx (context overflow is not transient), llama-server
    chat_template_kwargs only to local endpoints, OpenRouter provider pin
    and reasoning flag when configured."""
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("STATEMENTLEDGER_LLM_API_KEY") or os.environ.get(
        "OPENROUTER_API_KEY"
    )
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body: dict = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0,
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
from pathlib import Path

import statementledger as pl

spec = importlib.util.spec_from_file_location("candidate", Path("parser.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
verdict = pl.evaluate(module.parse, pl.load_frames(),
                      key=getattr(module, "KEY", None),
                      expect_ledgers={expect_ledgers})
print("GATE:" + json.dumps(verdict))
'''


def py_gate(workdir: Path, trace: Trace, pins: dict[str, int] | None = None) -> dict:
    """The engine gate: import workdir/parser.py in a fresh sandbox and run
    parse() on EVERY statement. Verdict shape matches evaluate(); the extra
    'gate_crashed' key says the child itself died (syntax error, timeout)."""
    (workdir / "gate_parser.py").write_text(
        GATE_RUNNER.format(expect_ledgers=repr(pins)))
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
    return None


# --- Trace ------------------------------------------------------------------------


class Trace:
    """Append-only run log next to the scripts and frames it describes."""

    def __init__(self, directory: Path):
        self.path = directory / "trace.log"
        self._file = self.path.open("a", encoding="utf-8")

    def log(self, section: str, body: str = "") -> None:
        self._file.write(
            f"\n{'=' * 78}\n[{time.strftime('%H:%M:%S')}] {section}\n"
            f"{'-' * 78}\n{body}\n"
        )
        self._file.flush()

    def line(self, text: str) -> None:
        self._file.write(f"{text}\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


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
    return sum(len(str(m.get("content", ""))) for m in messages)


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
    _schema("grep", "Which lines hold a term (case-insensitive; regex "
             "optional). The way to find labels and stop words before "
             "guessing them.", {
                 "stmt": {"type": "integer"},
                 "pattern": {"type": "string"},
                 "regex": {"type": "boolean", "description": "treat pattern as a case-insensitive regex"},
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
    if args.get("regex"):
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
    return json.dumps({"hits": shown, "count": len(hits),
                       "truncated": len(hits) > 40}, default=str)


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


def _tool_python(ctx: Ctx, args: dict) -> str:
    ctx.probes += 1
    (ctx.workdir / "probe.py").write_text(str(args.get("code", "")))
    outcome = run_script(ctx.workdir, "probe")
    parts = []
    if outcome["returncode"] != 0:
        parts.append(f"CRASHED:\n{outcome['stderr'][-1500:] or '(no stderr)'}")
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
    verdict = py_gate(ctx.workdir, ctx.trace, ctx.pins)
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
        return payload + "\nVERIFIED — reconciles every statement", "run"
    note = ""
    if crashed:
        site = crash_site((ctx.workdir / "gate_parser.out").read_text())
        note = f"\ngate crashed{f' in {site}' if site else ''} — the traceback is in the verdict errors"
    return payload + note, "run"


def dispatch(name: str, arguments: str, ctx: Ctx) -> tuple[str, str]:
    """Execute one tool call; returns (result text, kind) where kind is
    look/code/run. Never raises — failures are ERROR: strings the model
    reads."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except (ValueError, json.JSONDecodeError) as bad:
        return (f"ERROR: arguments are not valid JSON ({bad}) — resend the "
                "tool call"), "look"
    ctx.trace.log(f"tool call: {name}", json.dumps(args, default=str)[:2000])
    if name == "grep":
        return _tool_grep(ctx, args), "look"
    if name == "render":
        return _tool_render(ctx, args), "look"
    if name == "table":
        return _tool_table(ctx, args), "look"
    if name == "python":
        return _tool_python(ctx, args), "look"
    if name == "read":
        return _tool_read(ctx), "look"
    if name == "write":
        return _tool_write(ctx, args), "code"
    if name == "edit":
        return _tool_edit(ctx, args), "code"
    if name == "run":
        return _tool_run(ctx)
    return f"ERROR: unknown tool {name!r} (schemas are attached)", "look"


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
    frames = frames or frames_for(pdfs, workdir)
    statements = frames["statements"]
    first = statements[0]
    trace.log("frames", f"{len(statements)} statement(s); {first['name']}: "
              f"{len(first['lines'])} lines")
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

    for turn in range(1, turns + 1):
        saved = elide_middle(messages, threshold=compact_chars)
        if saved > 0:
            trace.log(f"turn {turn} context elided", f"{saved} chars saved")
        try:
            raw = chat(messages, TOOLS)
        except RuntimeError as error:
            chat_fails += 1
            trace.log(f"turn {turn} chat failed", str(error))
            history.append({"turn": turn, "outcome": f"chat failed: {str(error)[:120]}"})
            if chat_fails >= CHAT_FAIL_STREAK:
                break
            continue
        chat_fails = 0
        message = normalize_message(raw, turn)
        messages.append(message)

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
        for call in calls:
            function = call.get("function", {})
            result, kind = dispatch(function.get("name", ""),
                                    function.get("arguments", "{}"), ctx)
            kinds.append(kind)
            entry = {"role": "tool", "tool_call_id": call["id"], "content": result}
            messages.append(entry)
            last_tool = entry
            if kind == "run" and ctx.last_gate and ctx.last_gate.get("verified"):
                if last_tool is entry:
                    entry["content"] += f"\n\n{footer(ctx, turn, turns)}"
                trace.line(f"turn {turn}: VERIFIED via run")
                history.append({"turn": turn, "calls": [c.get("function", {}).get("name") for c in calls],
                                "outcome": "VERIFIED"})
                return {"key": ctx.last_gate.get("key"),
                        "code": (workdir / "parser.py").read_text()}, True, history

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
        trace.line(f"turn {turn}: {summary}")
        history.append({"turn": turn,
                        "calls": [c.get("function", {}).get("name") for c in calls],
                        "outcome": summary})

    return (ctx.best[1] if ctx.best else None), False, history


# --- CLI -----------------------------------------------------------------------------


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("pdfs", nargs="+", type=Path, help="statements from one bank")
    cli.add_argument("--turns", type=int, default=TURN_DEFAULT,
                     help="agent turns (default: %(default)s)")
    cli.add_argument("--no-save", action="store_true", help="never write the registry")
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

    system = AGENT_SYSTEM_PROMPT
    task_message = None
    require_key = None
    baseline = None
    pins = None
    if args.key:
        source_path = REGISTRY / f"{args.key}.py"
        if not source_path.exists():
            print(f"no registered parser {args.key!r} in {REGISTRY}")
            return 1
        baseline = source_path.read_text()
        (workdir / "parser.py").write_text(baseline)
        frames = frames_for(args.pdfs, workdir)
        verdict = py_gate(workdir, trace)
        verdict.pop("gate_crashed", None)
        print("baseline: " + ("VERIFIED" if verdict["verified"] else
                              f"{verdict['clean']}/{len(args.pdfs)} statements reconcile"))
        if verdict["verified"]:
            trace.log("result", "baseline already verifies every statement")
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
        trace.log("result", f"FAILED ({'no artifact' if artifact is None else 'not verified'})")
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
        print(f"maintained in place: {len(diff)} lines changed "
              "(git history is the parser's changelog)")
        trace.log("maintenance diff", "\n".join(diff[:80]))
    if args.no_save:
        print(f"\nVERIFIED (not saved, --no-save); parser drafted in {workdir}")
        trace.log("result", f"VERIFIED (not saved); parser drafted in {workdir}")
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

    REGISTRY.mkdir(exist_ok=True)
    out_path = REGISTRY / f"{key}.py"
    out_path.write_text(artifact["code"])
    print(f"\nVERIFIED parser saved: {out_path}")
    print("Reuse with zero LLM:")
    print(f"  uv run statementledger statement.pdf --key {key}")
    trace.log("result", f"VERIFIED parser saved: {out_path}")

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
