# statementledger web UI

A local, single-user web UI over the `statementledger` library. Accounts
are folders on disk, statement PDFs upload through the browser, parsing
goes through the parser registry, and verification failures appear as
outcomes with per-parser attempts. When no registered parser verifies a
statement, the UI launches a distill-agent run and shows it live.

The server lives in `web/` (`app.py`, `store.py`, `parsing.py`,
`ledger.py`, `runs.py`, `agent_events.py`, `pages.py`); the UI is the
React SPA in `web/frontend/`. None of it is part of the pip package.

Everything below describes the current implementation. API citations use
module-level symbols (`sl.parse` is in `statementledger/__init__.py`,
and so on).

## 1. Goals and non-goals

Goals:

- One account is one folder, and the folder is the source of truth.
- Uploading a PDF from a registered bank parses and verifies it with no
  configuration. The account remembers its parser key.
- Verification failures are visible outcomes: which parsers were tried,
  which check each one failed, and what to do next.
- Distill runs are launchable from the UI, observable live, cancellable,
  and survive page reloads and server restarts.

Non-goals, deliberate:

- No database and no migrations. The filesystem is the store.
- No authentication and no multi-user support. The server binds to
  localhost (see §10).
- No editing of parsed data. Correctness comes from the arithmetic gate;
  hand-fixing rows would defeat it.
- No PDF parsing in the browser and no LLM calls from the web process
  itself. The agent runs as a subprocess (§8.2).

## 2. Architecture

Two parts, one process:

- **Server.** FastAPI, one uvicorn worker, bound to 127.0.0.1. Parse
  routes are sync handlers (`def`, not `async def`), because pdfplumber
  blocks and FastAPI runs sync handlers in a threadpool. A per-account
  `threading.Lock` serializes parse-and-write (§10).
- **UI.** A React SPA (`web/frontend`: Vite, TypeScript, Tailwind,
  TanStack Query, react-router), built to static files. The server serves
  the bundle at `/` with assets under `/assets`, a history fallback for
  unknown browser GETs, and 308 redirects from the legacy `/app/*` paths.

**Content negotiation.** The SPA is the only UI and the server is its
JSON API. Page routes answer both kinds of caller on the same URL,
selected by the `Accept` header: browsers (`Accept: text/html`) get the
SPA shell, so deep links and refreshes resolve client-side; the app's
fetches (`Accept: application/json`) get the page's data. Without the
built bundle the server is API-only, and page GETs return a 503 that
points at `tools/serve_web.sh`.

**Response convention.** Protocol errors use real HTTP status codes
(400, 404, 409, 413) with JSON bodies. Domain outcomes such as "parsed",
"duplicate", and "no parser verified" are 200-level responses with a
`status` field in the body: a failed parse of a successfully stored
upload is a result, not a transport error.

**Storage location.** Everything the app writes goes under
`STMTLEDGER_WEB_DATA`: the repo-relative `data/` directory when unset
(gitignored), or `~/.local/share/statementledger` when launched through
`tools/serve_web.sh`, which also builds the SPA on first run.

**Dependencies and test seam.** The web stack sits in the `web`
dependency group; node and npm are needed only to build the SPA;
`uv build` still packages only `src/statementledger`. For tests and
browser-driven development, `create_app(harness_script=...)` (or the
`STMTLEDGER_WEB_HARNESS_SCRIPT` env var) replaces the agent with a
canned script, so the whole runs pipeline runs as a real subprocess
with no LLM.

## 3. Storage

Three concepts map onto disk one to one:

- **Account**: a real-world bank account the user holds. One folder.
  Several accounts may share one registry key (same bank, two accounts).
- **Statement**: one uploaded PDF plus its parse outcome, identified by
  a content hash. Re-uploads dedupe for free and the raw bytes are never
  rewritten.
- **Registry parser**: a key `cc__bank__product__n` and a pure `parse()`
  module. Parsers are code shared by all accounts; accounts are data.

### 3.1 Storage tree

```
<data>/                                  # STMTLEDGER_WEB_DATA (see §2)
├── accounts/
│   └── <slug>/
│       ├── account.json                 # metadata + registry key pin
│       └── statements/
│           └── <sha256[:16]>/
│               ├── original.pdf         # raw upload, immutable
│               ├── statement.json       # canonical dict; absent until a
│               │                        # verified parse
│               └── meta.json            # status, summary, attempts,
│                                        # append-only parse history
├── runs/
│   └── <id>/
│       ├── run.json                     # argv, pid, status, timestamps
│       └── events.jsonl                 # trajectory (§9.1)
├── registry/                            # user overlay: parsers distilled
│   ├── <key>.py                         # at runtime live here, shadowing
│   │                                    # same-key built-ins; user data,
│   │                                    # never committed
│   └── .history/<key>/
│       ├── objects/<sha16>.py           # immutable revision blobs
│       └── history.jsonl                # one record per save
└── cache/
    └── pages/<sha-prefix>/page_N.png    # lazily rasterized PDF pages
```

The agent writes its own working directory under
`logs/distill_<timestamp>_<stem>/` (gitignored), referenced from
`run.json`.

### 3.2 `account.json`

```json
{
  "slug": "bluelantern-savings",
  "display_name": "Blue Lantern Savings",
  "registry_key": "us__bluelantern__savings__1",
  "registry_key_source": "auto-detected",
  "created_at": "2026-10-09T10:00:00+00:00",
  "notes": ""
}
```

`registry_key` is the pin. When set, uploads parse with exactly that
parser (`sl.parse(pdf, key=...)`). When null, the account is in
detection mode (§7.2). `registry_key_source` records how the pin was
set: `"user-pinned"` or `"auto-detected"`. Unpinning returns the account
to detection mode.

### 3.3 `meta.json`

```json
{
  "id": "3f9c1a2b4d5e6f70",
  "sha256": "3f9c1a2b4d5e6f70...",
  "original_filename": "stmt_jan.pdf",
  "size_bytes": 84213,
  "uploaded_at": "2026-10-02T09:14:00+00:00",
  "status": "parsed",
  "period": {"start": "2026-01-01", "end": "2026-01-31"},
  "summary": {
    "transactions": 47,
    "opening_balance": 50000.0,
    "closing_balance": 11663.82
  },
  "history": [
    {"at": "2026-10-02T09:14:03+00:00", "outcome": "failed",
     "attempts": [
       {"key": "us__copperkettle__checking__1",
        "error": "reconcile: row 4 (...): running ... vs printed ..."},
       {"key": "us__bluelantern__savings__1", "error": ""}]},
    {"at": "2026-10-02T09:15:10+00:00", "outcome": "parsed",
     "parser_sha": "9b1deb4d3b7d4bec",
     "key": "us__bluelantern__savings__1",
     "statementledger": "0.1.0"}
  ]
}
```

`status` is one of `unparsed`, `parsed`, `failed`. `period` is derived
from the transaction dates (minimum and maximum of non-null dates; rows
may carry `date: null`, which `validate()` allows) and feeds the overlap
and chain checks (§10).

Design points:

- **Dedupe by id.** The same sha256 re-uploaded to the same account gets
  a 409 pointing at the existing statement. The same PDF in a different
  account is allowed; folders stay self-contained.
- **`statement.json` is derived data.** Re-parses overwrite it in place.
  Each pass appends one `history` entry with the key, the `parser_sha`
  of the exact revision that verified, and `statementledger.__version__`,
  so a stored ledger stays attributable to the code that produced it.
- **Version bumps are new keys.** Upgrading `us__bank__product__1` to
  `...__2` is a re-pin plus a re-parse of the account (§7.4). Overlay
  edits keep their own revision history (`registry/.history/`,
  inspected and reverted with `tools/registry_history.py`); built-in
  edits are a git pull.

## 4. The library surface the UI wraps

All of this is the package's public API; the web layer is a thin wrapper
around it.

### 4.1 Parsing with and without a key

```python
def parse(pdf: str, key: str | None = None) -> dict
```

With `key`, only that parser runs. With `key=None`, every registered key
is tried in order and the first result that verifies wins. Success
returns the canonical dict tagged with `_key`. Failure raises
`VerificationError` whose message is `no registered parser verified
<pdf.name>` followed by one line per candidate (`<key>: <error>`).
Attempt errors take three forms: `"TypeName: msg | HINT: ..."` for a
crash with remedial hint, `"schema: ..."` from `validate()`, and
`"reconcile: ..."` from the arithmetic check. The UI never string-parses
this exception; it uses the structured path in §4.4.

### 4.2 The canonical statement dict

Guaranteed by `validate()` against `CANONICAL_FIELDS`
(`statementledger.engine`):

```json
{
  "bank_name": "Blue Lantern Savings Bank",
  "account_number": "1234 5678",
  "opening_balance": 50000.0,
  "closing_balance": 11663.82,
  "transactions": [
    {"date": "2026-01-02", "description": "Opening deposit",
     "amount": 10000.0, "balance": 60000.0}
  ],
  "totals": [
    {"label": "Total deposits", "kind": "positive", "value": 12345.67}
  ],
  "_key": "us__bluelantern__savings__1"
}
```

`date` is an ISO string or null; `amount` is signed; `balance` may be
null; `totals` is optional. `validate()` checks the presence and types
of the canonical fields and ignores extra keys, which is what makes the
optional provenance convention of §9.5 possible without engine changes.

### 4.3 Verification surfaces

- `sl.parse()` only returns reconciled output. The check runs inside
  every call.
- `validate(stmt) -> list[str]`: schema errors; empty list means valid.
- `reconcile_statement(stmt, tol=TOL) -> str | None`: `None` when the
  arithmetic holds, otherwise the first divergence with row and drift
  detail. Cheap enough to power a "verify again" badge on the statement
  page without re-parsing.
- `sl.VerificationError`, and `sl.hint_for(message)` for remedial hints
  (already embedded in crash attempt errors).

### 4.4 Registry enumeration and structured verdicts

- `sl.keys() -> list[str]`: all registered keys, sorted, built-ins plus
  user overlay, globbed live on every call. The overlay resolves
  `STATEMENTLEDGER_USER_REGISTRY`, else `~/.local/share/statementledger/
  registry` (XDG-aware). The web layer always passes its own `registry/`
  tree explicitly.
- `sl.load_module(key, overlay=None)`: imports the overlay copy of
  `<key>.py` when one exists, else the built-in, and checks the `KEY` and
  `parse()` contract. It execs the file on every call with no import
  caching, so a parser saved by a distill run becomes usable by a
  running server without a restart. Same for `keys()`.
- Revision history (`statementledger.registry`, overlay only): 
  `save_parser(key, code, source, ...)` writes the immutable blob, swaps
  the live copy atomically, appends one `history.jsonl` record, and
  returns the sha16. `parser_history(key)`, `revision_code(key, sha16)`,
  `revert_parser(key, sha16)`, `parser_file(key)`, and
  `current_sha(key)` read back and restore revisions. `parser_sha` in
  `meta.json` (§3.3) comes from here.
- `sl.frames_for(pdfs, workdir) -> dict`: PDF to frames
  (`statementledger.frames`), returning `{"statements": [...]}`.
  `sl.parse` wraps this in a tempdir itself; the web layer manages
  frames only for the probe and overlay paths.
- `sl.evaluate(parse_fn, statements, key=None) -> dict`: the structured
  verdict, `{"key", "stage", "results": [{"name", "ok", "rows",
  "error"}], "clean", "verified"}`.

The detection probe (§7.2) is `parse()`'s loop unrolled: extract frames
once with `frames_for`, then `evaluate` each candidate module against
them, giving per-key results instead of one exception string.

### 4.5 CLI

`statementledger <pdf> [--key KEY]` (`statementledger.cli`). The distill
agent: `statementledger-distill <pdfs...> [--key K] [--turns N]
[--no-save] [--built-in]`, configured through the
`STATEMENTLEDGER_LLM_URL / MODEL / API_KEY / PROVIDER / REASONING /
TURNS / TIMEOUT` variables (`.env.example`). A verified run saves into
the user overlay with a revision record; `--built-in` writes into
`src/statementledger/registry/` for promotion into the curated set.

## 5. HTTP API

Browser GETs on page routes get the SPA shell (§2); the responses below
are what the SPA's JSON fetches receive.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/` | accounts list | |
| POST | `/accounts` | create account | form `display_name`, optional `registry_key`; 400 invalid, 409 slug exists |
| GET | `/accounts/{slug}` | account detail | statements, chain status, active runs |
| DELETE | `/accounts/{slug}` | delete account | 204 |
| POST | `/accounts/{slug}/key` | pin or unpin parser | empty key unpins; 400 unknown key |
| POST | `/accounts/{slug}/statements` | upload PDF | multipart `file`; stores PDF and meta first, parse outcome in the 201 body; 400 not a PDF, 413 over the size cap |
| POST | `/accounts/{slug}/statements/{sid}/parse` | re-parse one statement | account pin by default, `key` override in body |
| GET | `/accounts/{slug}/statements/{sid}` | statement detail | |
| GET | `/accounts/{slug}/statements/{sid}/original.pdf` | the stored PDF | immutable upload bytes |
| DELETE | `/accounts/{slug}/statements/{sid}` | delete statement | removes the folder |
| GET | `/accounts/{slug}/statements/{sid}/ledger.json` | canonical dict | 409 when not parsed yet |
| GET | `/accounts/{slug}/statements/{sid}/ledger.csv` | transactions CSV | 409 when not parsed yet |
| GET | `/accounts/{slug}/ledger.json` | merged account ledger | across parsed statements |
| POST | `/accounts/{slug}/probe` | dry-run try-all | uploaded file or stored `statement_id`; nothing persisted; per-key verdicts plus the verified set (a picker when ambiguous) |
| POST | `/accounts/{slug}/reparse` | re-parse all with the pin | each entry reports old and new status, period, summary; drift is flagged; 400 when unpinned |
| GET | `/accounts/{slug}/statements/{sid}/pages/{n}.png` | rasterized page | lazy, cached under `<data>/cache/pages/` |
| GET | `/accounts/{slug}/statements/{sid}/frames.json` | the statement's frames | overlay line coordinates index into this |
| GET | `/registry` | parser table | key, description, usage, revision provenance |
| GET | `/registry.json` | parser list | machine-readable form |
| POST | `/runs` | launch or attach | JSON or form: `account_slug`, `statement_ids`, `mode` (draft or maintain), `key?`, `turns?`, `workdir?` (`workdir` set means attach to an existing agent run); 409 while a run is active |
| GET | `/runs` | run list | |
| GET | `/runs/{id}` | run state | `run.json` contents |
| GET | `/runs/{id}/turns/{n}` | one turn of the trajectory | the parser.py snapshot as of that turn, its gate verdict, the summary, and the tool transcript |
| GET | `/runs/{id}/events` | SSE trajectory | one event per line, `id:` is the sequence number, `Last-Event-ID` / `?after=` resume; a terminal `run_status` event closes the stream even when the agent was killed |
| DELETE | `/runs/{id}` | cancel | 202 while cancelling |

## 6. Screens

### 6.1 Accounts list (`/`)

One card per account: display name, pinned key (or detection mode),
statement count, period span, chain status (`chain ok (11/11 links)` or
the offending link), latest closing balance, and last-updated time. A
header shows the registry size. Account creation is a small form:
display name plus an optional parser pin.

### 6.2 Account detail (`/accounts/{slug}`)

Header with the pinned key (with a change control) and the chain status.
An upload dropzone (PDF only, size-capped). A statements table: period,
transaction count, opening to closing balance, status per statement
(`parsed`, `overlap` warning, `no parser verified`, `unparsed`), and
per-row actions (view, re-parse, delete). Failed statements offer
"Register new bank...", which opens the run launch dialog (§6.4). A
distill-runs section lists this account's runs with a live watch link.

### 6.3 Upload outcomes

Three outcomes render inline after an upload. **Parsed**: period,
transaction count, the key that verified, and the reconciled balances,
with a link to the statement. **Duplicate**: a pointer to the existing
statement (same sha256). **No parser verified**: the stored-PDF notice,
a table of every attempted key with its specific error, the
"Register new bank..." action, a copy-paste CLI alternative
(`statementledger-distill <path to the stored original.pdf>`), and a
"Re-probe parsers" action for after a run has saved a new parser.

### 6.4 Run view (`/runs/{id}`)

The launch dialog collects the mode (draft for a new bank, or maintain
against the pinned key), the turn budget, and shows the model name from
the environment plus a cost note. The run view is a split pane: the
rasterized PDF with the selection overlay on the left; the turn
timeline, an expandable probe output tail, and the draft parser code on
the right. A status line carries the latest gate verdict and the model
name. Cancel is always one click.

### 6.5 Statement detail (`/accounts/{slug}/statements/{id}`)

A reconciliation banner (verified at parse time, with the key,
statementledger version, and parse timestamp), a "verify again" action
backed by `reconcile_statement`, balances and totals, the transactions
table, exports (JSON, CSV), the original PDF, re-parse and delete
actions, and the append-only parse history.

## 7. Upload and parse flow

### 7.1 Pinned account

1. `POST .../statements` receives the multipart file and runs the
   hygiene checks (§10): magic bytes, size, non-empty.
2. sha256 becomes the id. If the folder already exists, the response is
   the duplicate outcome.
3. The server stores `original.pdf` and `meta.json` with status
   `unparsed`. The upload is durable from this point on; no later
   failure can lose the file.
4. Parsing runs inline with the pinned key (§4.1). Success writes
   `statement.json`, sets status `parsed`, derives period and summary,
   and appends history. Failure records the structured attempts, sets
   status `failed`, and the response offers re-detection, a
   maintain-mode run, or the CLI.

### 7.2 Detection (unpinned account)

Frames are extracted once, then each registered module is `evaluate`d
against them (§4.4). Exactly one verifier parses, saves, and the UI
suggests pinning it (`registry_key_source: "auto-detected"`). Several
verifiers surface a picker; the UI never auto-pins an ambiguous case,
because the account association is permanent while `parse()`'s
first-wins rule is a one-off. No verifier stores the attempts and shows
the register-new-bank outcome (§6.3).

### 7.3 Probe without persistence

`POST .../probe` answers "what would happen" for an uploaded file or a
stored statement without writing anything. It backs the "Re-probe"
action after a run saves a new parser, and it is the dry-run behind
detection.

### 7.4 Re-parse

Per statement: `POST .../{sid}/parse`, the same code path as the upload
parse. Per account: `POST .../reparse` walks every stored statement with
the pin and reports old and new status, period, and summary; drift is
flagged loudly, since two verified parses can still differ in what they
read (for example, a `totals` check added in a later parser version). A
version bump is: pin the new key, then re-parse all. History keeps the
old key, so provenance stays auditable.

## 8. Job model

### 8.1 Parsing is synchronous

Frames extraction is seconds-fast for personal statements and
`evaluate` is milliseconds, and there is one user on localhost. Parsing
runs inside the request with a generous timeout (about 60 seconds).
There is no queue and no job table; a failed parse is a `status` field.
Upload and parse remain separate endpoints so a stored PDF can be
re-parsed later and a parse failure can never lose the upload, but the
upload handler calls the parse logic inline so the first response
carries the result.

### 8.2 Distill runs are the one background job

An agent run is minutes long, costs money, needs the
`STATEMENTLEDGER_LLM_*` environment, and executes model-written code in
a sandbox. It runs as a tracked, cancellable subprocess:

- **Spawn.** `[sys.executable, "-m", "statementledger.agent", <stored
  original.pdf paths...>, --turns N, --key K]` with cwd at the repo
  root, `.env` loaded through `statementledger.env` (the API key never
  reaches the browser; the server does not need the env at boot). The
  spawned environment pins `STATEMENTLEDGER_USER_REGISTRY` to this
  app's registry tree, so a verified parser saves into the app's own
  overlay and not some other instance's, and carries `STMTLEDGER_RUN_ID`
  so the parser's history record names the run.
- **Trajectory.** The agent writes an append-only `events.jsonl` beside
  its `trace.log` (`Trace.emit`). A watcher thread per run tails it into
  `<data>/runs/<id>/events.jsonl` and the app relays it over SSE
  (§9.4). `run.json` records argv, pid, account, statement ids, status,
  and timestamps, which makes runs reload-safe.
- **Attach mode.** A run started manually in a terminal, or an older
  workdir with only a `trace.log`, is attached by pointing the run at
  that workdir. `web/agent_events.py` (`TraceDeriver`) reconstructs the
  same event vocabulary from the artifacts. Attach is tail-watch only:
  no live selection events (§9.2).
- **Lifecycle.** One active run at a time; a second launch gets a 409.
  `DELETE /runs/{id}` sends SIGTERM to the process group and SIGKILL
  after a grace period, and the manager writes the terminal `cancelled`
  event itself. Terminal states are `converged`, `failed`, `cancelled`,
  and `orphaned` (pid gone on restart; artifacts stay browsable). On
  convergence the backend auto-parses the pending statements that
  triggered the run with the new key and offers to pin the account.
  Registry pickup needs no restart (§4.4).

## 9. Run view internals

### 9.1 Trajectory events

One JSON object per line in `events.jsonl`, relayed verbatim as SSE:

```json
{"type": "run_started", "model": "gpt-6-luna"}
{"type": "frames", "statements": 1, "name": "stmt_jan.pdf", "lines": 120}
{"type": "tool", "turn": 2, "name": "render", "args": {"start": 0, "n": 40},
 "result": "...first 4000 characters of the tool output..."}
{"type": "look", "turn": 2, "tool": "render", "label": "render 0+40",
 "page": 0, "rect": [72, 60, 503, 132], "line_ids": [0, 1]}
{"type": "parser", "turn": 3, "content": "KEY = ...\ndef parse(frames): ..."}
{"type": "gate", "turn": 3, "verdict": {"key": null, "stage": "parse",
 "clean": 0, "verified": false, "results": [{"name": "stmt.pdf", "ok": false,
 "rows": 0, "error": "schema: transactions[3]: amount must be a number"}]}}
{"type": "turn", "turn": 3, "summary": "0/1 clean", "chars_k": 9, "notes": []}
{"type": "elided", "turn": 7, "chars": 84210, "forced": false}
{"type": "chat_failed", "turn": 8, "error": "..."}
{"type": "converged", "key": "us__newbank__checking__1", "saved": true}
{"type": "failed", "reason": "turns exhausted (best: 0/1 statements clean)"}
```

The run manager appends `{"type": "cancelled"}` when a run is cancelled,
and the SSE relay closes every stream with a terminal `run_status` SSE
event, so the client learns of a dead agent even after a kill.

`gate.verdict` is the `sl.evaluate` result (§4.4); a turn-0 gate carries
`baseline: true` when it gates the staged starting parser rather than a
loop edit. `parser` events carry the full draft source after each write
or edit. The UI ignores unknown event types, so the schema is additive.

### 9.2 Look events (the overlay's data)

Every render, grep, or table call points at a statement location through
its arguments or result. The agent resolves that into a `look` event:
the frames lines the call examined, their page, and a bounding rectangle
in PDF points. The resolution is pure geometry over frames lines
(`statementledger.looks`); the model cooperates implicitly by calling
the tools, and there is no convention for it to follow.

- `render` with `start` and `n` selects lines `start` through
  `start + n - 1` (n clamped to at most 120).
- `grep` selects the line ids its result reports as hits.
- `table` with `start` and `stop` selects that span.

The rectangle extends 11 points above each line's baseline and 2 below,
because frames store the binned baseline, not glyph height. Calls that
point at no statement lines emit no look. The web layer derives the same
events for legacy workdirs through the same function
(`web/agent_events.py` imports `look_event`), so the live and derived
paths cannot drift; attach-mode runs show looks as the trace replays.

### 9.3 PDF rendering and coordinates

Pages rasterize server-side with `pypdfium2`, lazily, at about 144 dpi
(scale 2.0), cached content-addressed under `<data>/cache/pages/`. The
browser shows an image with absolutely positioned highlight elements.
(In-browser pdf.js was rejected as a large vendored blob with fiddly
text-layer transforms; PyMuPDF is AGPL; pypdfium2 is one wheel with a
bundled pdfium and keeps the coordinate math in Python where the frames
already live.)

Frames lines carry `{page, y, items: [[text, x1, x2], ...]}` in
pdfplumber points, with y measured downward from the page top, which
matches pdfium's top-left origin at natural scale. For a render scale
`s`: `px = x * s`, `py = y * s`, and a line highlight band is
`[min(x1)*s, (y - H)*s, max(x2)*s, (y + 2)*s]` with a band height
`H` of about 11 points (frames store the binned line bottom, not glyph
height). Line ids index into the statement's `frames.json`, served next
to the page images. A PDF with a non-zero cropbox origin or `/Rotate`
needs the corresponding offset or rotation applied once per document;
the synthetic fixtures have neither.

### 9.4 Transport and interaction

The SSE endpoint (`GET /runs/{id}/events`) relays one event per line
with the sequence number as the SSE id, so reconnects via
`Last-Event-ID` or `?after=` resume without gaps, and a terminal
`run_status` event closes the stream even when the agent died without
writing one. In the SPA, the stream folds into a reducer
(`web/frontend/src/pages/run/store.ts`); the overlay, timeline, and
code panes render from that state, so auto-follow, scrubbing, and code
fetches cannot race each other. Clicking a turn scrubs the overlay to
that turn's last selection; while live, auto-follow pins to the newest
event and any manual scrub unpins.

### 9.5 Provenance overlay (optional)

The same PDF-plus-overlay component can highlight which printed lines
produced a parsed value, on any statement page. Parsers may adopt an
optional convention, for example `txn["line"] = <frames line index>`
and `stmt["_anchors"] = {"opening_balance": 2, "closing_balance": 3}`;
`validate()` ignores extra keys (§4.2), so this needs no engine change
and roughly five lines per registry module. Without the convention the
UI simply shows no overlay.

## 10. Hygiene and edge cases

- **Upload validation.** Reject non-PDF by magic bytes (`%PDF-` within
  the first 1024 bytes; the extension is untrusted), reject empty
  files, cap size at 25 MiB by default (`STMTLEDGER_WEB_MAX_MB`). The
  original filename is kept in metadata only; the on-disk name is the
  hex id.
- **Encrypted or scanned PDFs.** An encrypted file fails with a plain
  message; frames with zero lines produce a "no extractable text,
  scanned image PDF?" hint pointing at `tools/heal_pdf.py` for
  damaged-but-text PDFs.
- **Duplicates.** Same sha256 in the same account: 409 plus a link. In
  a different account: allowed, with an informational note.
- **Period overlaps.** Derived periods are compared pairwise; an overlap
  or gap between adjacent statements is a warning chip. Banks reissue
  corrected periods, so it never blocks.
- **Chain check.** Adjacent statements must satisfy
  `prev.closing == next.opening` within `TOL = 0.01`; the account header
  names the offending link.
- **Path safety.** Slugs are server-generated from the display name
  (lowercase, `[^a-z0-9]+` to `-`, trimmed, at most 64 characters),
  must match `^[a-z0-9][a-z0-9-]{0,63}$`, and are additionally checked
  with `Path.resolve().is_relative_to(accounts_root)`. Statement ids
  are hex from sha256. No user string becomes a path component.
- **Localhost only, no auth.** The server binds 127.0.0.1 by default.
  There is no authentication, no CSRF protection, and no multi-user
  support, deliberately: statements are sensitive financial data. Do
  not port-forward or run on a shared host. A single bearer token plus
  an opt-in `--host` is the escape hatch if exposure is ever needed.
- **Git hygiene.** All PDFs are gitignored except the synthetic test
  fixtures; a repo-relative `data/` store and `logs/` are ignored too.
  Parsed JSON contains real transaction data and must never be
  committable by accident.
- **Concurrency.** One user, but two tabs can race. A per-account lock
  serializes parse-and-write; statement writes are idempotent because
  they are content-addressed. One uvicorn worker.
- **Cost guard.** The launch dialog shows the model name (from the
  environment) and the turn budget; one active run at a time; cancel is
  always one click away.
