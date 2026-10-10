"""REPL-distill harness for statementledger registry parsers.

The model never emits a parser blindly and never sees a verification loop.
Each round it emits a complete PROBE SCRIPT against the statement's layout
frames (statementledger as pl). The harness runs the script in a sandboxed
subprocess and feeds back exactly what the script printed — head-style
views, format checks, a local reconcile — plus, once the script defines
parse(), the engine's evaluation of it (validate + reconcile on EVERY
statement).

Loop per round:
    model emits full python script (chat)
      -> AST lint (transcription guard: no money-shaped literals)
      -> run in sandboxed child (timeout, rlimits, no network, clean env)
      -> script printed probes + local reconcile?  -> stdout is the feedback
      -> script defines def parse(frames)? -> engine gate on EVERY statement
      -> all verified? save to the registry : build feedback and revise

The end state: a registered parser module (KEY + pure parse()) that parses
future statements of the same bank in milliseconds with zero LLM. The
scripts are scaffolding: kept under the run directory for audit, never
registered.

Usage (from the repo root):
    uv run python tools/distill_harness.py statement1.pdf [statement2.pdf ...]
    uv run python tools/distill_harness.py --script my_probe.py stmt.pdf
    uv run python tools/distill_harness.py --key us__bank__prod__1 old.pdf new.pdf
        # MAINTAIN: evolve the registered parser until ALL statements verify
        # (old ones are the regression set); starts from the registry source
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from envloader import load_env

load_env()

import statementledger
from statementledger.frames import frames_for
from statementledger.registry import REGISTRY, key_problem

# LLM endpoint (STATEMENTLEDGER_LLM_* in .env / environment; see .env.example)
BASE_URL = os.environ.get("STATEMENTLEDGER_LLM_URL", "").rstrip("/")
MODEL = os.environ.get("STATEMENTLEDGER_LLM_MODEL", "")
TIMEOUT = int(os.environ.get("STATEMENTLEDGER_LLM_TIMEOUT", "300"))

SCRIPT_TIMEOUT = 60  # wall clock per probe script, seconds
OUTPUT_TAIL = 12000  # chars of script stdout/stderr fed back to the model
# (real statements are long; the model debugs through this window)
MAX_TOKENS = 100_000  # probe scripts are longer than config JSON
# Advisory loop policy knobs (see also --crash-streak / --compact-chars):
CRASH_STREAK = int(os.environ.get("STATEMENTLEDGER_CRASH_STREAK", "3"))
COMPACT_CHARS = int(os.environ.get("STATEMENTLEDGER_COMPACT_CHARS", "120000"))
# (compaction at 120k chars ≈ 30k tokens: lands ~round 8 of a 30-round run,
# re-triggers every few rounds; lower it for small-context local models)

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


# The full system message: system + response contract + worked example,
# composed by assembly.j2.md via includes.
PY_SYSTEM_PROMPT = render_prompt("assembly.j2.md")


GATE_RUNNER = '''import importlib.util
import json
from pathlib import Path

import statementledger as pl

spec = importlib.util.spec_from_file_location("candidate", Path("{candidate}.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
verdict = pl.evaluate(module.parse, pl.load_frames(),
                      key=getattr(module, "KEY", None),
                      expect_ledgers={expect_ledgers})
print("GATE:" + json.dumps(verdict))
'''


def py_gate(workdir: Path, stem: str, trace: Trace,
            pins: dict[str, int] | None = None) -> dict:
    """The engine gate: import workdir/<stem>.py in a fresh sandbox and run
    parse() on EVERY statement. Verdict shape matches evaluate(). Works for
    round scripts (round_N) and the maintain-mode baseline alike. `pins`
    (maintain mode only) carries the per-statement ledger counts of the
    previously registered version, so maintenance cannot quietly drop or
    split an account."""
    (workdir / f"gate_{stem}.py").write_text(
        GATE_RUNNER.format(candidate=stem, expect_ledgers=repr(pins)))
    outcome = run_script(workdir, f"gate_{stem}")
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
    if verdict.get("verified"):
        problem = key_problem(verdict.get("key") or "")
        if problem:
            verdict["verified"] = False
            verdict["results"].append({
                "name": "key", "ok": False, "rows": 0,
                "error": problem,
            })
    trace.log(
        f"{stem} statementledger gate",
        f"exit {outcome['returncode']}\n{json.dumps(verdict, default=str)}",
    )
    return verdict


def py_feedback(evaluation: dict) -> str:
    """Per-statement verdict lines for a round."""
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


# --- py maintain mode: evolve a registered parser, never from scratch ---------
# (prompts: tools/prompts/maintain_addendum.j2.md + maintain_task.j2.md)


# --- LLM transport (raw code, not json_object) --------------------------------


def chat_code(messages: list[dict]) -> str:
    """Raw-code chat completion with retry. Works against the local
    llama-server and OpenAI-compatible clouds (OpenRouter et al): Bearer
    auth from STATEMENTLEDGER_LLM_API_KEY / OPENROUTER_API_KEY, and the
    llama-server chat_template_kwargs switch is sent just to local
    endpoints — remote providers 400 on unknown body fields. Free-tier
    endpoints answer with in-band error payloads or 429s; those retry with
    backoff instead of killing the run."""
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
    if re.match(r"https?://(127\.|localhost|\[::1\])", BASE_URL):
        body["chat_template_kwargs"] = {"enable_thinking": False}
    # OpenRouter routing controls (no-ops elsewhere, absent unless set):
    # STATEMENTLEDGER_LLM_PROVIDER pins endpoint(s), comma-separated, e.g.
    # "amazon-bedrock/us-east-1" -> provider.only + no fallbacks (dodges
    # guardrail-excluded endpoints); STATEMENTLEDGER_LLM_REASONING=true
    # enables reasoning for models that want it.
    provider_pin = os.environ.get("STATEMENTLEDGER_LLM_PROVIDER", "").strip()
    if provider_pin:
        body["provider"] = {
            "only": [p.strip() for p in provider_pin.split(",") if p.strip()],
            "allow_fallbacks": False,
        }
    if os.environ.get("STATEMENTLEDGER_LLM_REASONING", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        body["reasoning"] = {"enabled": True}

    delays = (0, 5, 15, 45)
    last_error = "unknown"
    # Accept both conventions: the API root (…/v1, to which /chat/completions
    # is appended) or the full endpoint path as written in .env.
    endpoint = BASE_URL.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode(),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                payload = json.load(response)
            content = payload.get("choices", [{}])[0].get("message", {}).get("content")
            if content and content.strip():
                return content
            last_error = f"empty/absent content: {json.dumps(payload)[:300]}"
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


def extract_code(text: str) -> str | None:
    """The python code block in a response (tolerant: any fence, or a bare
    script that starts with an import or the # probe marker)."""
    match = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    if match:
        return match.group(1).strip()
    if text.lstrip().startswith(("# probe", "import ", "from ", "#!")):
        return text.strip()
    return None


# --- Transcription guard -------------------------------------------------------

MONEY_LITERAL_RE = re.compile(r"^\$?\d{1,3}(,\d{3})+(\.\d{2})?\s*(CR|DR)?$", re.I)


def lint_code(code: str) -> str | None:
    """Reject scripts that smuggle statement data in as literals, and broad
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
                "let the script crash; the harness feeds the exact traceback "
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
                "transcribing statement values into the script is forbidden; "
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


# --- Sandboxed execution -------------------------------------------------------


def _child_limits() -> None:
    """Best-effort rlimits for the sandboxed child. Each is applied
    independently and skipped where the platform refuses it — macOS rejects
    RLIMIT_AS outright and low NOFILE ceilings below its default soft limit.
    An exception in preexec would kill subprocess.run with a detail-free
    SubprocessError, so nothing here may raise."""
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
            capture_output=True,
            timeout=5,
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


# --- Trace ---------------------------------------------------------------------


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


# --- Loop policy (pure; unit-tested) -------------------------------------------


def shown_indices(stdout: str) -> list[int]:
    """Line indices a round actually displayed: show()-style dump rows
    (038 ["text",x1,x2]), render() wall rows (038 | text), and dict-print
    probes (0 {'a': 1}) all count. The look-coverage advisory reads these."""
    return [int(index) for index in
            re.findall(r"^(\d{1,4}) [\[{|]", stdout, re.M)]


def crash_site(stderr: str, script: str) -> str | None:
    """The innermost traceback frame inside the model's own script, e.g.
    "parse (round_7.py:23)" — the place a probe should aim at. Falls back
    to the last File frame; None when there is no traceback."""
    frames = re.findall(r'File "(?:.*/)?([^"/]+)", line (\d+), in (\w+)',
                        stderr)
    return _site(frames, script)


def _site(frames: list[tuple[str, str, str]], script: str) -> str | None:
    for name, line, fn in reversed(frames):
        if name == script:
            return f"{fn} ({name}:{line})"
    if frames:
        name, line, fn = frames[-1]
        return f"{fn} ({name}:{line})"
    return None


def crash_streak_advisory(streak: int, limit: int, site: str | None) -> str | None:
    """Fires when full scripts crashed `limit` times in a row (and again at
    every multiple): the model is guessing — send it looking instead."""
    if limit <= 0 or streak < limit or streak % limit:
        return None
    where = f", in `{site}`" if site else ""
    return (
        f"ADVISORY: {streak} full scripts in a row have crashed{where}. "
        "Guessing the same line again will crash the same way. Return a "
        "burner probe (first line '# probe') that prints what feeds that "
        "failure — pl.render around the offending region, pl.grep for the "
        "label you keep trying — read it, then return the fixed script."
    )


def commit_nudge(round_number: int, rounds: int, probes_done: int,
                 max_probes: int | None) -> str | None:
    """The only end-of-budget pressure: past 90% of the turns (or past the
    advisory probe budget when one is set), full scripts are the only thing
    that can converge the run. Advisory, never a bounce."""
    if round_number >= 0.9 * rounds:
        return ("Advisory: 90% of the turns are used — only a full script "
                "can converge the run. Return KEY + def parse(frames) + the "
                "demo next; keep any prints you need inside it.")
    if max_probes is not None and probes_done >= max_probes:
        return ("Advisory: probe budget reached — probe inside the full "
                "script instead: KEY + def parse(frames) with prints, then "
                "the demo under if __name__ == '__main__'.")
    return None


def look_advisory(top_seen: bool, tail_seen: bool, already_shown: bool) -> str | None:
    """One-time, never-blocking: the words that terminate the table live at
    the END of the statement; a model that has not seen top and tail is
    guessing them."""
    if already_shown or (top_seen and tail_seen):
        return None
    missing = []
    if not top_seen:
        missing.append("TOP")
    if not tail_seen:
        missing.append("END")
    return (
        "Advisory: your rounds have not yet shown the " + " and ".join(missing)
        + " of the statement — the words that terminate the table live at "
        "the end. pl.render(lines, 10, start=len(lines) - 10) shows it."
    )


# --- Compaction -----------------------------------------------------------------


def message_chars(messages: list[dict]) -> int:
    return sum(len(str(m.get("content", ""))) for m in messages)


def split_keep(messages: list[dict],
                keep_assistants: int = 3) -> tuple[list, list, list]:
    """(prefix, middle, suffix): prefix = system + task; suffix = from the
    keep_assistants-th assistant message from the end onward, verbatim;
    middle = everything between — the span compaction may elide."""
    assistants = [i for i, m in enumerate(messages)
                  if m.get("role") == "assistant"]
    cut = assistants[-keep_assistants] if len(assistants) >= keep_assistants \
        else len(messages)
    return messages[:2], messages[2:cut], messages[cut:]


SUMMARIZE_PROMPT = """You are summarizing superseded rounds of your own \
bank-statement parser REPL loop (scripts + harness feedback follow). Write \
the digest the next round needs, as plain-text bullets under 1200 chars:
- labels found and their line indices (account number, opening/closing \
balance, statement period)
- header row words + its line index; column names in x order
- stop words that end the transaction table + line index
- money/date format quirks seen (CR/DR, parens, grouping, yearless dates)
- per failed round: what was tried and the exact failure (crash site, \
reconcile drift, schema error)
- key line indices worth re-showing (wrapped rows, section markers, \
repeated headers)
No code, no preamble."""


def summarize_middle(middle: list[dict], chat) -> str:
    """One chat call turning the elided span into the digest above."""
    transcript = "\n\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in middle)
    reply = chat([{"role": "user", "content": SUMMARIZE_PROMPT +
                  "\n\n" + transcript}])
    return reply[:2000]


def compact_messages(messages: list[dict], threshold: int = COMPACT_CHARS,
                     chat=None, trace: Trace | None = None) -> int:
    """Summarize superseded conversation bulk once it crosses `threshold`
    chars; below it, nothing is elided at all.

    Full scripts are stateless by design — each round re-emits the whole
    script — so an old script's value is near zero while being both the
    biggest token mass and the parrot bait the model copies instead of
    thinking. What IS worth keeping is the observations (labels found,
    header words, stop words, line indices), so the elided span becomes a
    model-written digest instead of silent truncation. The system+task
    prefix and the last 3 assistant turns stay verbatim; when the digest
    call fails, fall back to placeholder/truncation on the middle only.
    Mutates `messages` in place; returns chars saved."""
    if message_chars(messages) <= threshold:
        return 0
    prefix, middle, suffix = split_keep(messages)
    if not middle:
        return 0
    before = message_chars(messages)
    replacement: list[dict] = []
    if chat is not None:
        try:
            digest = summarize_middle(middle, chat)
            replacement = [{
                "role": "user",
                "content": "(earlier rounds summarized — full text elided; "
                           "the newest rounds below are verbatim)\n" + digest,
            }]
        except RuntimeError:
            digest = None
    if not replacement:
        for m in middle:  # fallback: the old elision semantics, middle only
            if m.get("role") == "assistant" and len(str(m.get("content", ""))) > 500:
                m["content"] = ("(an earlier full script was here; omitted "
                                "to save context — write the current script "
                                "fresh, guided by the newest feedback)")
            elif m.get("role") == "user" and "End of probe" in str(m.get("content", "")) \
                    and len(str(m.get("content", ""))) > 700:
                m["content"] = m["content"][:700] + "\n(older probe output truncated)"
        replacement = middle
    messages[:] = prefix + replacement + suffix
    saved = before - message_chars(messages)
    if trace is not None:
        which = "digest" if len(replacement) == 1 else "fallback elision"
        trace.log("compaction", f"{which}: {saved} chars saved")
    return saved


def script_feedback(outcome: dict) -> str:
    stdout = outcome["stdout"][-OUTPUT_TAIL:]
    stderr = outcome["stderr"][-1500:]
    parts = []
    if outcome["returncode"] != 0:
        parts.append(
            "The script CRASHED — fix exactly this error and keep everything "
            f"else identical:\n{stderr or '(no stderr)'}"
        )
    if stdout.strip():
        parts.append(f"Your script printed:\n{stdout}")
    elif outcome["returncode"] == 0:
        parts.append(
            "The script printed nothing — print() your probes; that output "
            "is your only view of the statement."
        )
    return "\n\n".join(parts)


def draft_by_repl(
    pdfs: list[Path],
    workdir: Path,
    rounds: int,
    trace: Trace,
    canned_script: Path | None = None,
    system_prompt: str = PY_SYSTEM_PROMPT,
    task_message: str | None = None,
    require_key: str | None = None,
    max_probes: int | None = None,
    pins: dict[str, int] | None = None,
    crash_streak_limit: int = CRASH_STREAK,
    compact_chars: int = COMPACT_CHARS,
    chat=chat_code,
) -> tuple[dict | None, bool, list[dict]]:
    """Returns ({key, code} or None, verified?, round history). Maintain mode
    (require_key set) pins KEY to the registered parser being evolved;
    task_message replaces the drafted task template (it embeds the baseline
    source and its gate verdict). Loop policy is advisory-only: probes are
    always allowed; nudges (commit, crash-streak, look-coverage) ride along
    with the normal feedback."""
    frames = frames_for(pdfs, workdir)
    first = frames["statements"][0]
    trace.log(
        "frames",
        f"{len(frames['statements'])} statement(s); {first['name']}: "
        f"{len(first['lines'])} lines",
    )

    messages: list[dict] = []
    history: list[dict] = []
    best: tuple[int, dict] | None = None
    previous_code: str | None = None
    consecutive_probes = 0
    probes_done = 0
    tail_seen = False
    top_seen = False
    look_hint_done = False
    crash_streak = 0
    total_lines = len(first["lines"])
    chars_in = 0
    chars_out = 0

    for round_number in range(rounds):
        started = time.perf_counter()
        if canned_script is not None:
            code = canned_script.read_text()
        else:
            task = task_message or render_prompt(
                "task.j2.md",
                name=first["name"],
                pages=1 + max(line["page"] for line in first["lines"]),
                lines=len(first["lines"]),
            )
            if not messages:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": task},
                ]
            saved = compact_messages(messages, threshold=compact_chars,
                                     chat=chat, trace=trace)
            if saved > 0:
                trace.log(f"round {round_number} context compacted", f"{saved} chars saved")
            try:
                raw = chat(messages)
            except RuntimeError as error:
                trace.log(f"round {round_number} chat failed", str(error))
                print(f"round {round_number}: chat failed, skipping ({str(error)[:90]})")
                history.append(
                    {"round": round_number, "outcome": f"chat failed: {str(error)[:120]}"}
                )
                continue
            chars_out += len(raw)
            code = extract_code(raw)
            trace.log(f"round {round_number} raw model response", raw)
            if code is None:
                feedback = (
                    "Your response contained no ```python code block. Return "
                    "ONE complete script in a single fence."
                )
                messages.append({"role": "assistant", "content": raw[:2000]})
                messages.append({"role": "user", "content": feedback})
                trace.log(f"round {round_number} feedback sent", feedback)
                history.append({"round": round_number, "outcome": "no code block"})
                print(f"round {round_number}: no code block in response")
                continue
            messages.append({"role": "assistant", "content": code})

        violation = lint_code(code)
        if violation:
            trace.log(f"round {round_number} lint rejected", violation)
            print(f"round {round_number}: lint rejected ({violation[:70]})")
            if canned_script is not None:
                history.append({"round": round_number, "outcome": f"lint: {violation}"})
                return None, False, history
            feedback = (
                f"REJECTED before running: {violation}\nReturn the complete "
                "corrected script."
            )
            messages.append({"role": "user", "content": feedback})
            history.append({"round": round_number, "outcome": f"lint: {violation}"})
            continue

        # Burner probe: looking turns, allowed at any time — probing is the
        # fastest way to learn the layout. Discarded by design: nothing is
        # distilled, nothing is evaluated, and convergence still requires one
        # complete script through the full engine gate.
        is_probe = code.lstrip().splitlines()[0].strip().lower() == "# probe"
        (workdir / f"round_{round_number}.py").write_text(code)

        if is_probe:
            consecutive_probes += 1
            probes_done += 1
            outcome = run_script(workdir, f"round_{round_number}")
            # Look coverage: render()/show() print zero-padded line indices
            # at the start of each row; an index near the document ends means
            # that region was actually displayed.
            shown = shown_indices(outcome["stdout"])
            if shown:
                tail_seen = tail_seen or max(shown) >= total_lines - 8
                top_seen = top_seen or min(shown) <= 8
            trace.log(
                f"round {round_number} probe output",
                f"exit {outcome['returncode']}\n{outcome['stdout'][-4000:]}"
                f"\n--- stderr ---\n{outcome['stderr'][-2000:]}",
            )
            verdict = "probe ok" if outcome["returncode"] == 0 else "probe crashed"
            print(f"round {round_number}: {verdict} (burner, #{consecutive_probes})")
            history.append({"round": round_number, "outcome": f"{verdict} (burner)"})
            parts = [script_feedback(outcome)]
            warning = commit_nudge(round_number, rounds, probes_done, max_probes) \
                or "Probe again, or return the complete full script."
            parts.append(
                "End of probe (discarded — nothing was distilled). " + warning
            )
            feedback = "\n\n".join(parts)
            trace.log(f"round {round_number} probe feedback sent", feedback)
            messages.append({"role": "user", "content": feedback})
            continue
        consecutive_probes = 0

        if require_key:
            declared = re.search(r'^KEY\s*=\s*["\']([^"\']+)["\']', code, re.M)
            if not declared or declared.group(1) != require_key:
                feedback = (
                    f"MAINTAIN MODE: KEY must stay {require_key!r}"
                    + (f", but the script declares {declared.group(1)!r}"
                       if declared else ", but the script declares none")
                    + " — you are evolving the registered parser, not "
                    "registering a new one. Return the corrected script."
                )
                messages.append({"role": "user", "content": feedback})
                history.append({"round": round_number, "outcome": "key renamed"})
                trace.log(f"round {round_number} rejected", feedback)
                continue

        outcome = run_script(workdir, f"round_{round_number}")
        crashed = outcome["returncode"] != 0
        # Crash streak: consecutive full-script crashes. Probes never reset
        # it — probing is the desired response to the advisory. Any full
        # script that runs clean (gate verdict or not) resets it.
        crash_streak = crash_streak + 1 if crashed else 0
        shown = shown_indices(outcome["stdout"])
        if shown:
            tail_seen = tail_seen or max(shown) >= total_lines - 8
            top_seen = top_seen or min(shown) <= 8
        trace.log(
            f"round {round_number} script output",
            f"exit {outcome['returncode']} in {outcome['elapsed']:.1f}s\n"
            f"{outcome['stdout'][-4000:]}\n--- stderr ---\n{outcome['stderr'][-2000:]}",
        )
        artifact = None
        if outcome["returncode"] == 0 and "def parse(" in code:
            evaluation = py_gate(workdir, f"round_{round_number}", trace, pins)
            if evaluation.get("clean", 0) > 0:
                artifact = {"key": evaluation.get("key"), "code": code}
        else:
            evaluation = None

        elapsed = time.perf_counter() - started
        if evaluation is not None and evaluation.get("verified"):
            summary = "VERIFIED"
        elif evaluation is not None:
            summary = f"{evaluation.get('clean', 0)}/{len(pdfs)} clean"
        else:
            summary = ("script crashed" if outcome["returncode"] != 0
                       else "no parse() exercised")

        print(f"round {round_number}: {summary} ({elapsed:.1f}s)")
        history.append({"round": round_number, "outcome": summary})
        trace.line(f"round {round_number}: {summary} ({elapsed:.1f}s)")

        if evaluation is not None and evaluation.get("verified"):
            return artifact, True, history
        if evaluation is not None and evaluation["clean"] > 0:
            if best is None or evaluation["clean"] > best[0]:
                best = (evaluation["clean"], artifact)

        if canned_script is not None:
            return (best[1] if best else artifact), False, history

        # Assemble the REPL feedback: the script's own reconcile verdict is
        # the loudest signal (a FAIL it ignored explains most gate
        # failures), then the engine verdict, then what it printed.
        parts = []
        if "reconcile: FAIL" in outcome["stdout"]:
            parts.append(
                "YOUR OWN reconcile printed FAIL — the drift names what is "
                "wrong (a stop term cutting the table early, a dropped row, "
                "a wrong sign, a mis-binned column). Fix parse() until it "
                "prints OK."
            )
        if evaluation is not None:
            parts.append(py_feedback(evaluation))
        elif outcome["returncode"] != 0:
            last = (outcome["stderr"].strip().splitlines() or [""])[-1]
            hint = statementledger.hint_for(last)
            if hint:
                parts.append(f"HINT: {hint}")
        elif "def parse(" not in code:
            parts.append(
                "The script never defines def parse(frames) — the "
                "deliverable is the parser module: KEY, parse(), demo "
                "under if __name__ == '__main__'."
            )
        parts.append(script_feedback(outcome))
        # Advisories ride along with the feedback; they never block a round.
        streak_note = crash_streak_advisory(
            crash_streak, crash_streak_limit,
            crash_site(outcome["stderr"], f"round_{round_number}.py")
            if crashed else None)
        if streak_note:
            parts.append(streak_note)
            trace.line(f"round {round_number}: crash-streak advisory "
                       f"({crash_streak} in a row)")
        look_note = look_advisory(top_seen, tail_seen, look_hint_done)
        if look_note:
            look_hint_done = True
            parts.append(look_note)
        feedback = "\n\n".join(parts) + "\n\nReturn the complete corrected script."
        chars_in += len(feedback)
        trace.log(f"round {round_number} feedback sent", feedback)

        if code == previous_code:
            trace.line(
                f"round {round_number}: escalation — context reset (model "
                "parroted its previous script)"
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Your previous script failed. The diagnosis:\n"
                        f"{feedback[-3000:]}\n\nRewrite the complete script "
                        "fixing exactly what the diagnosis names. Probes "
                        "first, KEY + def parse last."
                    ),
                },
            ]
        else:
            messages.append({"role": "user", "content": feedback})
        previous_code = code

    trace.log(
        "token accounting (chars)",
        f"responses: {chars_out}, feedback sent: {chars_in}",
    )
    return (best[1] if best else None), False, history


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("pdfs", nargs="+", type=Path, help="statements from one bank")
    cli.add_argument(
        "--rounds",
        type=int,
        default=int(os.environ.get("STATEMENTLEDGER_LLM_ROUNDS", "30")),
        help="REPL turns (each is a full script + engine gate; default 30)",
    )
    cli.add_argument(
        "--script",
        type=Path,
        default=None,
        help="run this probe script instead of asking the model (canned test)",
    )
    cli.add_argument("--no-save", action="store_true", help="never write the registry")
    cli.add_argument(
        "--key",
        default=None,
        help="MAINTAIN the registered parser with this key — start from the "
             "registry source, not from scratch; the gate requires ALL given "
             "statements to verify (pass old AND new ones: the old are the "
             "regression set)",
    )
    cli.add_argument(
        "--max-probes",
        type=int,
        default=None,
        help="advisory probe budget: past it (and from 90%% of turns) probe "
             "feedback carries a commit-now note — probes are never bounced "
             "(default: unlimited)",
    )
    cli.add_argument(
        "--crash-streak",
        type=int,
        default=CRASH_STREAK,
        help="after this many consecutive full-script crashes, the feedback "
             "gains a probe-suggestion naming the failing function "
             "(default: %(default)s)",
    )
    cli.add_argument(
        "--compact-chars",
        type=int,
        default=COMPACT_CHARS,
        help="summarize (not truncate) the conversation once it exceeds this "
             "many chars; system+task and the last 3 assistant turns stay "
             "verbatim (default: %(default)s)",
    )
    args = cli.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    workdir = Path("logs") / f"distill_{stamp}_{args.pdfs[0].stem[:30]}"
    workdir.mkdir(parents=True, exist_ok=True)
    trace = Trace(workdir)

    mode = f"canned script {args.script}" if args.script else f"{MODEL} at {BASE_URL}"
    print(f"distill harness: {len(args.pdfs)} statement(s), {mode}")
    print(f"workdir: {workdir}")
    trace.log("run start", f"mode={mode}\npdfs={[str(p) for p in args.pdfs]}")

    system = PY_SYSTEM_PROMPT  # system + contract + example (assembly.j2.md)

    # Maintain mode: stage the registered parser as the baseline, gate it
    # against the full statement set (old statements = regression set), and
    # hand the model the source + per-statement verdict as its starting point.
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
        (workdir / "baseline.py").write_text(baseline)
        frames = frames_for(args.pdfs, workdir)
        verdict = py_gate(workdir, "baseline", trace)
        print(
            "baseline: " + ("VERIFIED" if verdict["verified"] else
                            f"{verdict['clean']}/{len(args.pdfs)} statements reconcile")
        )
        if verdict["verified"]:
            trace.log("result", "baseline already verifies every statement")
            print(f"{args.key} already verifies all {len(args.pdfs)} "
                  "statement(s) — nothing to maintain.")
            trace.close()
            return 0
        # What the previous working version returned per statement is the
        # coverage expectation for every round: maintenance may restructure,
        # but never to fewer (or split) ledgers than the version being
        # replaced — for the statements that version already verified.
        pins = {result["name"]: result["ledgers"] for result in verdict["results"]
                if result["ok"]}
        if pins:
            trace.log("coverage pins", json.dumps(pins))
            print(f"coverage pinned from baseline: {pins}")
        first = frames["statements"][0]
        task_message = render_prompt(
            "maintain_task.j2.md",
            key=args.key,
            names=", ".join(p.name for p in args.pdfs),
            name=first["name"],
            pages=1 + max(line["page"] for line in first["lines"]),
            lines=len(first["lines"]),
            verdict=py_feedback(verdict),
            code=baseline,
        )
        system += "\n\n" + render_prompt("maintain_addendum.j2.md")
        require_key = args.key

    artifact, verified, history = draft_by_repl(
        args.pdfs, workdir, args.rounds, trace, canned_script=args.script,
        system_prompt=system,
        task_message=task_message, require_key=require_key,
        max_probes=args.max_probes, pins=pins,
        crash_streak_limit=args.crash_streak, compact_chars=args.compact_chars,
    )
    if artifact is None or not verified:
        trace.log("result", f"FAILED ({'no artifact' if artifact is None else 'not verified'})")
        trace.log("round history", json.dumps(history, indent=2))
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
        # nothing is registered, so the reuse demo runs the drafted module
        # straight from the workdir
        import importlib.util

        draft = workdir / "verified.py"
        draft.write_text(artifact["code"])
        spec = importlib.util.spec_from_file_location("verified_draft", draft)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        frames = frames_for(args.pdfs, workdir)["statements"]
        for pdf, stmt_frames in zip(args.pdfs, frames):
            for ledger in statementledger.ledgers_of(module.parse(stmt_frames)):
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
        for ledger in statementledger.parse_all(str(pdf), key=key):
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
