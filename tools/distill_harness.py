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
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from envloader import load_env

load_env()

import statementledger
from statementledger.frames import frames_for
from statementledger.registry import KEY_RE, REGISTRY

# LLM endpoint (STATEMENTLEDGER_LLM_* in .env / environment; see .env.example)
BASE_URL = os.environ.get("STATEMENTLEDGER_LLM_URL", "").rstrip("/")
MODEL = os.environ.get("STATEMENTLEDGER_LLM_MODEL", "")
TIMEOUT = int(os.environ.get("STATEMENTLEDGER_LLM_TIMEOUT", "300"))

SCRIPT_TIMEOUT = 60  # wall clock per probe script, seconds
OUTPUT_TAIL = 12000  # chars of script stdout/stderr fed back to the model
# (real statements are long; the model debugs through this window)
MAX_TOKENS = 100_000  # probe scripts are longer than config JSON

PY_SYSTEM_PROMPT = """You are a data scientist writing bank-statement parsers
in plain Python. A statement arrives as FRAMES — extracted layout, plain data:

    {"name": ..., "lines": [{"page": int, "y": int, "items": [[text, x1, x2], ...]}, ...]}

y grows downward; items on a line are in print order. A ledger is a geometry
problem: the header row names the columns, every item's x-center falls under
one of them, and label values (account number, opening/closing balance) sit
on the same line as their label. Plain loops and re solve it.

Your deliverable — a complete parser module. Nothing else persists:

    KEY = "us__bank__product__1"     # registry key, lowercase
    def parse(frames) -> dict:       # pure: derives everything from frames
        ...
        return {
            "bank_name": str,
            "account_number": str,
            "opening_balance": float,
            "closing_balance": float,
            "transactions": [
                {"date": "YYYY-MM-DD" or None,  # yearless tokens: take the
                 "description": str,             # year from the statement period
                 "amount": float,                # SIGNED: credits +, debits -
                 "balance": float or None},      # printed running balance, if any
            ],
            # OPTIONAL, but bind it when the statement prints section totals
            # ("Total deposits and other additions", "Total withdrawals ...").
            # Each total is an INDEPENDENT accuracy check — kind "positive"
            # matches Σ positive amounts, "negative" matches Σ |negatives|, "all"
            # matches the plain sum:
            "totals": [
                {"label": "Total deposits and other additions",
                 "kind": "positive", "value": 1234.56},
            ],
        }

Rules of the artifact:
- parse() may import ONLY the stdlib (re, datetime, json, collections, ...)
  plus statementledger. No files, network, or processes; no
  try/except — errors must crash loudly.
- No literals for printed amounts, dates, or transaction descriptions (the
  lint rejects them). Label TERMS like "Opening balance:" are bank
  furniture — literals are fine and expected.
- It must generalize across months of THIS bank: derive columns from each
  statement's own header row and stop terms from the words it prints; never
  hardcode one month's geometry or contents.
- Presentation semantics: a date-less row inherits the previous row's date;
  a balance-less row gets the running computed balance (start from opening,
  add each amount in order, ROUNDING TO 2 DECIMALS after every step — money
  is exactly 2dp, never 17800.230000000003). Never leave None where the
  statement's own arithmetic supplies the value — downstream consumers want
  a complete ledger, not holes the printer happened to leave.

Sandbox environment (import and go):
- import statementledger as pl
- THE DOCSTRINGS ARE THE MANUAL: every helper's full contract, examples and
  failure modes are one print away — print(pl.parse_amount.__doc__),
  print(pl.reconcile.__doc__), help(pl).
- pl.load_frames() -> the statements' frames LIST (frames.json in cwd).
  parse() receives ONE element of it: a single statement dict
  {"name": ..., "lines": [...]} — the #1 drafting crash is treating frames
  as something else.
- pl.show(lines, n=15, start=0) — layout slice with line indices
- pl.items(frames) -> [{text, x1, x2, cx, y, page, line}] per printed item
- pl.parse_amount("$1,234.56 DR") -> signed float (CR +, DR -, parens
  negate); None for unreadable tokens — None is NOT zero, it means "no
  money here" (a description-wrap line)
- pl.parse_date("05 Mar", default_year=2026) -> "2026-03-05"; None if not
  date-shaped
- pl.reconcile(amounts, opening, closing, balances=None) — the ledger check;
  must print OK before you finish

Verification is arithmetic, not opinion: the harness imports your module and
runs parse() on EVERY statement given; each must yield a valid canonical dict
AND reconcile (opening + sum(amounts) == closing; printed balances must track
when present; bound totals must match their parsed sums — they catch what
end-to-end reconcile cannot: a dropped offsetting pair or a double sign
flip still balances). Every gate error is remedial — crashes carry a HINT,
schema errors name the row and the fix, reconcile failures name the
divergent row. Read the error; it describes exactly what to change. End the
script with a demo guarded by if __name__ == "__main__": parse the first
statement, print it, reconcile.
"""

PY_CONTRACT = """Contract for every response:
- ONE complete runnable python script in a single ```python fence: imports,
  KEY, def parse(frames), then the demo guarded by if __name__ == "__main__".
- KEEP IT SHORT: under ~70 lines. No try/except anywhere — an error must
  crash; the harness feeds the exact traceback back to you.
- Literals only for label terms / column header words — never for printed
  amounts, dates, or transaction descriptions.
- BURNER PROBES: to just LOOK before committing, first line exactly
  "# probe" (print anything). Probes are discarded and uncapped, but they
  cannot converge the run; from 90% of turns used onward you are warned.
- MANDATORY LOOKING: your first full script is not accepted until probes
  have shown the TOP and the END of the statement — the words that
  terminate the table are at the end. Read them, never guess.
- Never finish while your own pl.reconcile prints FAIL — the drift names
  what is wrong (a stop term cutting the table early, a dropped row, a
  wrong sign)."""

PY_EXAMPLE = '''import re

import statementledger as pl

KEY = "us__demo__checking__1"


def parse(frames):
    lines = frames["lines"]

    def beside(label):                      # value printed right of a label
        for line in lines:
            for text, x1, _x2 in line["items"]:
                if label in text:
                    right = [t for t, a, _b in line["items"] if a > x1]
                    if right:
                        return " ".join(right)
        raise ValueError(f"label not printed: {label}")

    account = beside("Account No:")
    opening = pl.parse_amount(beside("Beginning Balance"))
    closing = pl.parse_amount(beside("Ending Balance"))
    year = int(re.search(r"\\b(19|20)\\d{2}\\b", beside("Statement Period")).group())

    header = next(line for line in lines                 # the column header row
                  if {"Date", "Amount"} <= {i[0] for i in line["items"]})
    cols = sorted(((t, (x1 + x2) / 2) for t, x1, x2 in header["items"]),
                  key=lambda c: c[1])
    names = [n for n, _ in cols]
    centers = [c for _, c in cols]
    edges = ([float("-inf")] + [(a + b) / 2 for a, b in zip(centers, centers[1:])]
             + [float("inf")])

    def column(cx):                        # which header an item sits under
        for name, low, high in zip(names, edges, edges[1:]):
            if low <= cx < high:
                return name
        return names[-1]

    transactions, pending_desc, pending_date = [], "", None
    for line in lines[lines.index(header) + 1:]:
        if "End of activity" in pl.line_text(line):      # stop term, READ it
            break
        row = {}
        for text, x1, x2 in line["items"]:
            name = column((x1 + x2) / 2)
            row[name] = (row.get(name, "") + " " + text).strip()
        if not any(row.values()):
            continue
        date = pl.parse_date(row.get("Date", ""), default_year=year)
        amount = pl.parse_amount(row.get("Amount", ""), strict_grouping=True)
        if amount is None:                  # no money: description wrap line
            pending_date = date or pending_date
            pending_desc = (pending_desc + " " + row.get("Description", "")).strip()
            continue
        transactions.append({
            "date": date or pending_date,
            "description": (pending_desc + " " + row.get("Description", "")).strip(),
            "amount": amount,
            "balance": pl.parse_amount(row.get("Balance", "")),
        })
        pending_desc, pending_date = "", None

    return {"bank_name": beside("Bank"), "account_number": account,
            "opening_balance": opening, "closing_balance": closing,
            "transactions": transactions}


if __name__ == "__main__":
    statement = parse(pl.load_frames()[0])
    print(statement["account_number"], statement["opening_balance"],
          "->", statement["closing_balance"])
    for txn in statement["transactions"][:5]:
        print(txn)
    pl.reconcile([t["amount"] for t in statement["transactions"]],
                 statement["opening_balance"], statement["closing_balance"],
                 [t["balance"] for t in statement["transactions"]])
'''

PY_TASK_TEMPLATE = """Statement to parse: {name} ({pages} pages, {lines} layout
lines). frames.json in the working directory holds it; pl.load_frames() reads
it.

Explore until the layout is clear (labels, header row, column spans, the
words that end the table, wrapped-description rows), then write the parser:
KEY + def parse(frames) returning the canonical dict, ending with the demo
under if __name__ == "__main__": print the parsed statement and reconcile.
Return the complete script in one ```python fence.

When unsure what a label or stop word says, first return a BURNER PROBE —
first line "# probe", print what you need (pl.show(...) slices). It is
discarded; then return the full script."""


GATE_RUNNER = '''import importlib.util
import json
from pathlib import Path

import statementledger as pl

spec = importlib.util.spec_from_file_location("candidate", Path("{candidate}.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
verdict = pl.evaluate(module.parse, pl.load_frames(),
                      key=getattr(module, "KEY", None))
print("GATE:" + json.dumps(verdict))
'''


def py_gate(workdir: Path, stem: str, trace: Trace) -> dict:
    """The engine gate: import workdir/<stem>.py in a fresh sandbox and run
    parse() on EVERY statement. Verdict shape matches evaluate(). Works for
    round scripts (round_N) and the maintain-mode baseline alike."""
    (workdir / f"gate_{stem}.py").write_text(GATE_RUNNER.format(candidate=stem))
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
        if not KEY_RE.match(verdict.get("key") or ""):
            verdict["verified"] = False
            verdict["results"].append({
                "name": "key", "ok": False, "rows": 0,
                "error": (f"KEY {verdict.get('key')!r} must be "
                          "cc__bank__product__n, lowercase"),
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
            lines.append(f"  {result['name']}: OK — {result['rows']} transactions")
        else:
            lines.append(f"  {result['name']}: FAILED — {result['error']}")
    return ("statementledger gate (parse() re-run on EVERY statement; schema "
            "+ reconcile required):\n" + "\n".join(lines))


# --- py maintain mode: evolve a registered parser, never from scratch ---------

MAINTAIN_ADDENDUM = """MAINTENANCE MODE. You are revising an EXISTING registered parser that
already works for at least one statement. Rules of maintenance:
- KEY stays exactly as given — never rename, never register a new key.
- Make the SMALLEST change that fixes the failing statement(s) WITHOUT
  breaking the passing one(s). The gate runs parse() on EVERY statement;
  all must verify. Prefer a new fallback branch or a widened condition over
  a restructure; keep what already works working.
- Diagnose from the baseline verdict first: it names each failing statement
  and the exact error (crash hint, schema row, or reconcile divergence).
  Read it before touching anything.
- When a statement prints section totals ("Total deposits and other
  additions", ...), bind them into totals[] — independent accuracy checks
  that catch errors end-to-end reconcile mathematically cannot see.
- You already hold the layout knowledge in the baseline code; probe (first
  line '# probe', as always) only what the baseline doesn't explain — e.g.
  the failing statement's header row, its stop words, its money formats."""

MAINTAIN_TASK_TEMPLATE = """Maintaining the registered parser {key}.
Statement set: {names}. frames.json in the working directory holds them all
(first: {name}, {pages} pages, {lines} layout lines).

Baseline verdict (the CURRENT registry code, run on every statement):
{verdict}

Current registered source:
```python
{code}
```

Tweak it minimally so EVERY statement verifies. Keep KEY = "{key}" exactly.
Return the complete revised script in one ```python fence; probe first when
the baseline doesn't explain a failure."""


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


# --- Trajectory (optional NDJSON event stream for the web UI, §9.1) -------------


SELECT_BAND = 11  # pt: highlight band height around a frames line (y is the
                  # binned bottom edge, not glyph height — one knob, tuned once)


class Trajectory:
    """One JSON object per line, flushed as it happens (--jsonl-trajectory).
    The web backend tails this file and relays it over SSE to the live run
    view. Nothing here runs without the flag — the CLI is unchanged.
    Consumers ignore unknown event types: the schema is additive."""

    def __init__(self, path: Path | None):
        self._file = (path.open("a", encoding="utf-8") if path is not None
                      else None)

    @property
    def enabled(self) -> bool:
        return self._file is not None

    def emit(self, type_: str, **fields) -> None:
        if self._file is None:
            return
        event = {"type": type_, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        event.update(fields)
        self._file.write(json.dumps(event, default=str) + "\n")
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()


def resolve_selection(stdout: str, lines: list[dict]) -> dict | None:
    """What the probe was LOOKING at, as data: the optional SELECT: line the
    probe printed — SELECT:{"page": P, "lines": [start, end), "label": …} —
    resolved against the statement's frames lines into {page, line_ids, rect,
    label}. Without a SELECT: line, falls back to the line indices pl.show()
    printed. rect is in pdfplumber points (y downward), page top-left origin,
    so the web overlay maps it as px = pt * render_scale."""
    selection = None
    for match in re.finditer(r"^SELECT:(\{.*\})\s*$", stdout, re.M):
        try:
            selection = json.loads(match.group(1))
        except ValueError:
            continue
    ids: list[int] = []
    page = 0
    label = ""
    if selection:
        page = int(selection.get("page") or 0)
        span = selection.get("lines") or []
        if len(span) == 2:  # half-open [start, end)
            ids = list(range(max(0, span[0]), min(len(lines), span[1])))
        elif span and isinstance(span[0], int):  # explicit id list
            ids = [i for i in span if isinstance(i, int) and 0 <= i < len(lines)]
        label = str(selection.get("label") or "")
    else:
        shown = [int(index) for index in
                 re.findall(r"^(\d{1,4}) [\[{]", stdout, re.M)]
        if not shown:
            return None
        ids = list(range(min(shown), max(shown) + 1))
        label = "probe show() output"
    ids = [i for i in ids if lines[i]["page"] == page]
    if not ids:
        return None
    x1 = min(item[1] for i in ids for item in lines[i]["items"]) if any(
        lines[i]["items"] for i in ids) else 0
    x2 = max(item[2] for i in ids for item in lines[i]["items"]) if any(
        lines[i]["items"] for i in ids) else 0
    top = min(lines[i]["y"] for i in ids) - SELECT_BAND
    bottom = max(lines[i]["y"] for i in ids) + 2
    return {"page": page, "line_ids": ids,
            "rect": [x1, top, x2, bottom], "label": label}


SELECT_PROMPT = """Probes may end with ONE extra line that names the region just examined:

SELECT:{"page": 0, "lines": [8, 35], "label": "transaction rows"}

(a half-open line-index range into pl.load_frames()[0]['lines'], same indices
pl.show() prints). It is optional data for the live run view — nothing is
graded on it; print it only when the region is genuinely what you looked at."""


# --- The loop ------------------------------------------------------------------


def compact_messages(messages: list[dict]) -> int:
    """Elide superseded conversation bulk so a 30-turn loop fits the context.

    Full scripts are stateless by design — each round re-emits the whole
    script — so an old script's value is near zero while being both the
    biggest token mass and the parrot bait the model copies instead of
    thinking. Keep the newest 2 full scripts verbatim; older ones become a
    placeholder. Probe outputs older than the last 3 are truncated (the
    observations stay, in abbreviated form). Returns chars saved."""
    before = sum(len(str(m.get("content", ""))) for m in messages)
    full_script_indexes = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "assistant"
        and isinstance(m.get("content"), str)
        and not m["content"].lstrip().startswith("# probe")
        and len(m["content"]) > 500
    ]
    for i in full_script_indexes[:-2]:
        messages[i]["content"] = (
            "(an earlier full script was here; omitted to save context — "
            "write the current script fresh, guided by the newest feedback)"
        )
    probe_feedback_indexes = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and "End of probe" in m["content"]
        and len(m["content"]) > 700
    ]
    for i in probe_feedback_indexes[:-3]:
        messages[i]["content"] = messages[i]["content"][:700] + "\n(older probe output truncated)"
    return before - sum(len(str(m.get("content", ""))) for m in messages)


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
    task_template: str = PY_TASK_TEMPLATE,
    task_message: str | None = None,
    require_key: str | None = None,
    enforce_look_gate: bool = True,
    max_probes: int | None = None,
    trajectory: "Trajectory | None" = None,
) -> tuple[dict | None, bool, list[dict]]:
    """Returns ({key, code} or None, verified?, round history). Maintain mode
    (require_key set) pins KEY to the registered parser being evolved;
    task_message replaces the drafted task template (it embeds the baseline
    source and its gate verdict)."""
    events = trajectory if trajectory is not None else Trajectory(None)
    frames = frames_for(pdfs, workdir)
    first = frames["statements"][0]
    trace.log(
        "frames",
        f"{len(frames['statements'])} statement(s); {first['name']}: "
        f"{len(first['lines'])} lines",
    )
    events.emit("frames", statements=[
        {"name": s["name"], "pages": 1 + max((l["page"] for l in s["lines"]),
                                             default=-1),
         "lines": len(s["lines"])} for s in frames["statements"]])

    messages: list[dict] = []
    history: list[dict] = []
    best: tuple[int, dict] | None = None
    previous_code: str | None = None
    consecutive_probes = 0
    probes_done = 0
    tail_seen = False
    total_lines = len(first["lines"])
    chars_in = 0
    chars_out = 0

    for round_number in range(rounds):
        started = time.perf_counter()
        events.emit("round_started", round=round_number)
        if canned_script is not None:
            code = canned_script.read_text()
        else:
            task = task_message or task_template.format(
                name=first["name"],
                pages=1 + max(line["page"] for line in first["lines"]),
                lines=len(first["lines"]),
            )
            if not messages:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": task},
                ]
            saved = compact_messages(messages)
            if saved > 0:
                trace.log(f"round {round_number} context compacted", f"{saved} chars elided")
            try:
                raw = chat_code(messages)
            except RuntimeError as error:
                trace.log(f"round {round_number} chat failed", str(error))
                events.emit("chat_failed", round=round_number,
                            error=str(error)[:400])
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
                events.emit("feedback_sent", round=round_number,
                            summary="no code block in response")
                history.append({"round": round_number, "outcome": "no code block"})
                print(f"round {round_number}: no code block in response")
                continue
            messages.append({"role": "assistant", "content": code})
            events.emit("model_output", round=round_number, chars=len(raw))

        violation = lint_code(code)
        if violation:
            trace.log(f"round {round_number} lint rejected", violation)
            events.emit("lint_rejected", round=round_number,
                        violation=violation)
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

        # Burner probe: looking turns. Discarded by design —
        # nothing is distilled, nothing is evaluated, and convergence still
        # requires one complete script through the full engine gate.
        is_probe = code.lstrip().splitlines()[0].strip().lower() == "# probe"
        (workdir / f"round_{round_number}.py").write_text(code)
        events.emit("round_saved", round=round_number,
                    kind="probe" if is_probe else "script",
                    path=f"round_{round_number}.py")

        # Commit latch: once 90% of the turn budget is spent, probes close
        # for good — the run must converge on full scripts. The look-gate is
        # waived from the same point so a model that never probed cannot
        # deadlock between "probe first" and "probes are closed".
        commit_mode = round_number >= int(rounds * 0.9)

        # Handling gate: the first full script is only accepted after the
        # model has actually LOOKED — at least one probe, and at least one
        # probe that showed the END of the statement (detectable: show()
        # prints line indices, so a high index in probe stdout means the tail
        # was on screen). Small models guess stop words unless forced to read
        # them; this is the forcing. Waived once the probe budget is spent —
        # otherwise bounces and the gate deadlock (the model can neither
        # probe again nor commit).
        budget_spent = (max_probes is not None and probes_done >= max_probes)
        if (
            not is_probe
            and not commit_mode
            and not budget_spent
            and canned_script is None
            and enforce_look_gate
            and (probes_done < 1 or not tail_seen)
        ):
            if probes_done < 1:
                instruction = (
                    "Return a burner probe (first line exactly '# probe') "
                    "printing the TOP of the statement: "
                    "pl.show(pl.load_frames()[0]['lines'], 12)."
                )
                reason = "no probe yet"
            else:
                instruction = (
                    "Your probes have not shown the END of the statement — "
                    "the words that terminate the transaction table live "
                    "there. Return a burner probe (first line '# probe'): "
                    "pl.show(lines, 6, len(lines) - 6), and READ what "
                    "follows the last transaction row."
                )
                reason = "tail not probed"
            feedback = (
                "NOT ACCEPTED — look before you write. Small models that "
                "guess labels and stop words instead of reading them fail; "
                f"{instruction}"
            )
            trace.log(f"round {round_number} full script gated", f"{reason}\n" + feedback)
            events.emit("feedback_sent", round=round_number,
                        summary=f"look gate: {reason}")
            print(f"round {round_number}: full script gated ({reason})")
            messages.append({"role": "user", "content": feedback})
            history.append(
                {"round": round_number, "outcome": f"full script gated: {reason}"}
            )
            continue
        # Probe budget: small models ignore the 90% warning and probe forever
        # (the local 4B spent 29 of 30 turns probing, one full script, zero
        # gate feedback). With a budget set, probes beyond it are BOUNCED —
        # not executed — with commit-now feedback.
        if (is_probe and max_probes is not None and probes_done >= max_probes
                and canned_script is None):
            feedback = (
                f"PROBE BUDGET ({max_probes}) EXHAUSTED — you have seen "
                "enough. Return the COMPLETE full script now: KEY + def "
                "parse(frames) + the demo guarded by if __name__ == "
                "'__main__'. It does not need to be perfect; the gate runs "
                "it, and the error names exactly what to fix next round."
            )
            messages.append({"role": "user", "content": feedback})
            history.append(
                {"round": round_number, "outcome": f"probe bounced (budget {max_probes})"}
            )
            trace.log(f"round {round_number} probe bounced", "budget exhausted")
            events.emit("feedback_sent", round=round_number,
                        summary=f"probe bounced (budget {max_probes})")
            continue
        if is_probe:
            consecutive_probes += 1
            probes_done += 1
            outcome = run_script(workdir, f"round_{round_number}")
            events.emit("round_ran", round=round_number,
                        returncode=outcome["returncode"],
                        elapsed=round(outcome["elapsed"], 2),
                        stdout_tail=outcome["stdout"][-400:])
            # Tail coverage: show() prints zero-padded line indices at the
            # start of each row; an index near the document end means the
            # stop-word region was actually displayed.
            shown = [
                int(index)
                for index in re.findall(
                    r"^(\d{1,4}) [\[{]", outcome["stdout"], re.M)
            ]
            if shown and max(shown) >= total_lines - 8:
                tail_seen = True
            selection = resolve_selection(outcome["stdout"], first["lines"])
            if selection:
                events.emit("probe_select", round=round_number, **selection)
            trace.log(
                f"round {round_number} probe output",
                f"exit {outcome['returncode']}\n{outcome['stdout'][-4000:]}"
                f"\n--- stderr ---\n{outcome['stderr'][-2000:]}",
            )
            verdict = "probe ok" if outcome["returncode"] == 0 else "probe crashed"
            print(f"round {round_number}: {verdict} (burner, #{consecutive_probes})")
            history.append({"round": round_number, "outcome": f"{verdict} (burner)"})
            parts = [script_feedback(outcome)]
            warning = (
                "WARNING: 90% of the turns are used — probes cannot converge "
                "the run. Return the COMPLETE full script next (keep needed "
                "prints inside it, verify with pl.reconcile)."
                if commit_mode
                else "Probe again, or return the complete full script."
            )
            parts.append(
                "End of probe (discarded — nothing was distilled). " + warning
            )
            feedback = "\n\n".join(parts)
            trace.log(f"round {round_number} probe feedback sent", feedback)
            events.emit("feedback_sent", round=round_number,
                        summary="probe feedback (see trace)")
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
                events.emit("feedback_sent", round=round_number,
                            summary=f"MAINTAIN: KEY must stay {require_key}")
                continue

        outcome = run_script(workdir, f"round_{round_number}")
        events.emit("round_ran", round=round_number,
                    returncode=outcome["returncode"],
                    elapsed=round(outcome["elapsed"], 2),
                    stdout_tail=outcome["stdout"][-400:])
        trace.log(
            f"round {round_number} script output",
            f"exit {outcome['returncode']} in {outcome['elapsed']:.1f}s\n"
            f"{outcome['stdout'][-4000:]}\n--- stderr ---\n{outcome['stderr'][-2000:]}",
        )
        artifact = None
        if outcome["returncode"] == 0 and "def parse(" in code:
            evaluation = py_gate(workdir, f"round_{round_number}", trace)
            events.emit("gate", round=round_number, verdict=evaluation)
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
        feedback = "\n\n".join(parts) + "\n\nReturn the complete corrected script."
        chars_in += len(feedback)
        trace.log(f"round {round_number} feedback sent", feedback)
        events.emit("feedback_sent", round=round_number,
                    summary=parts[0][:200] if parts else "")

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
        help="burner-probe budget; probes beyond it are bounced with "
             "commit-now feedback (small models ignore the 90%% warning and "
             "probe forever; default: unlimited)",
    )
    cli.add_argument(
        "--jsonl-trajectory",
        type=Path,
        default=None,
        help="append one JSON event per line to this file as the run happens "
             "(rounds, gate verdicts, probe selections) — the web UI's live "
             "run view tails it; without the flag nothing is written",
    )
    args = cli.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    workdir = Path("logs") / f"distill_{stamp}_{args.pdfs[0].stem[:30]}"
    workdir.mkdir(parents=True, exist_ok=True)
    trace = Trace(workdir)
    trajectory = Trajectory(args.jsonl_trajectory)

    def _cancelled(signum, frame):  # noqa: ARG001 - signal handler signature
        trajectory.emit("cancelled")
        trajectory.close()
        trace.close()
        sys.exit(130)

    if trajectory.enabled:
        signal.signal(signal.SIGTERM, _cancelled)
        signal.signal(signal.SIGINT, _cancelled)

    mode = f"canned script {args.script}" if args.script else f"{MODEL} at {BASE_URL}"
    print(f"distill harness: {len(args.pdfs)} statement(s), {mode}")
    print(f"workdir: {workdir}")
    trace.log("run start", f"mode={mode}\npdfs={[str(p) for p in args.pdfs]}")
    trajectory.emit("run_started", pdfs=[p.name for p in args.pdfs],
                    mode="maintain" if args.key else "draft",
                    model=(f"canned script {args.script.name}"
                           if args.script else MODEL),
                    rounds=args.rounds)

    system = (PY_SYSTEM_PROMPT + "\n\n" + PY_CONTRACT
              + "\n\nA complete parser for a different (demo) bank — "
                "imitate its shape and length, not its labels:\n"
                "```python\n" + PY_EXAMPLE + "\n```"
                + "\n\n" + SELECT_PROMPT)
    template = PY_TASK_TEMPLATE

    # Maintain mode: stage the registered parser as the baseline, gate it
    # against the full statement set (old statements = regression set), and
    # hand the model the source + per-statement verdict as its starting point.
    task_message = None
    require_key = None
    look_gate = True
    baseline = None
    if args.key:
        source_path = REGISTRY / f"{args.key}.py"
        if not source_path.exists():
            print(f"no registered parser {args.key!r} in {REGISTRY}")
            return 1
        baseline = source_path.read_text()
        (workdir / "baseline.py").write_text(baseline)
        frames = frames_for(args.pdfs, workdir)
        verdict = py_gate(workdir, "baseline", trace)
        trajectory.emit("gate", round="baseline", verdict=verdict)
        print(
            "baseline: " + ("VERIFIED" if verdict["verified"] else
                            f"{verdict['clean']}/{len(args.pdfs)} statements reconcile")
        )
        if verdict["verified"]:
            trace.log("result", "baseline already verifies every statement")
            print(f"{args.key} already verifies all {len(args.pdfs)} "
                  "statement(s) — nothing to maintain.")
            trajectory.emit("converged", key=args.key, path=str(
                REGISTRY / f"{args.key}.py"), saved=False,
                note="baseline already verifies every statement")
            trajectory.close()
            trace.close()
            return 0
        first = frames["statements"][0]
        task_message = MAINTAIN_TASK_TEMPLATE.format(
            key=args.key,
            names=", ".join(p.name for p in args.pdfs),
            name=first["name"],
            pages=1 + max(line["page"] for line in first["lines"]),
            lines=len(first["lines"]),
            verdict=py_feedback(verdict),
            code=baseline,
        )
        system += "\n\n" + MAINTAIN_ADDENDUM
        require_key = args.key
        look_gate = False  # the baseline IS the looked-at evidence; probe at will

    artifact, verified, history = draft_by_repl(
        args.pdfs, workdir, args.rounds, trace, canned_script=args.script,
        system_prompt=system, task_template=template,
        task_message=task_message, require_key=require_key,
        enforce_look_gate=look_gate, max_probes=args.max_probes,
        trajectory=trajectory,
    )
    if artifact is None or not verified:
        trace.log("result", f"FAILED ({'no artifact' if artifact is None else 'not verified'})")
        trace.log("round history", json.dumps(history, indent=2))
        best_clean = 0
        for entry in history:
            match = re.match(r"(\d+)/(\d+) clean", entry.get("outcome", ""))
            if match:
                best_clean = max(best_clean, int(match.group(1)))
        trajectory.emit("failed", reason=(
            f"rounds exhausted (best: {best_clean}/{len(args.pdfs)} "
            "statements clean)"))
        trajectory.close()
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
        trajectory.emit("converged", key=key, path=str(workdir / "verified.py"),
                        saved=False)
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
            stmt = module.parse(stmt_frames)
            line = (
                f"  {pdf.name}: {len(stmt['transactions'])} transactions, "
                f"opening {stmt['opening_balance']:,.2f} -> "
                f"closing {stmt['closing_balance']:,.2f}"
            )
            print(line)
            trace.line(line)
        trajectory.close()
        trace.close()
        return 0

    REGISTRY.mkdir(exist_ok=True)
    out_path = REGISTRY / f"{key}.py"
    out_path.write_text(artifact["code"])
    print(f"\nVERIFIED parser saved: {out_path}")
    print("Reuse with zero LLM:")
    print(f"  uv run statementledger statement.pdf --key {key}")
    trace.log("result", f"VERIFIED parser saved: {out_path}")
    trajectory.emit("converged", key=key, path=str(out_path), saved=True)

    for pdf in args.pdfs:
        stmt = statementledger.parse(str(pdf), key=key)
        line = (
            f"  {pdf.name}: {len(stmt['transactions'])} transactions, "
            f"opening {stmt['opening_balance']:,.2f} -> "
            f"closing {stmt['closing_balance']:,.2f}"
        )
        print(line)
        trace.line(line)
    trajectory.close()
    trace.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
