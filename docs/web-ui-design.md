# statementledger web UI — design

Status: implemented (Phases 1–3, 2026-10-09). The phased plan in §11 is
complete: `web/` + `tests/test_web.py` against the real fixtures; the
provenance convention of §9.5 is implemented UI-side (degrades gracefully)
but not yet adopted by any registry module. Date: 2026-10-09.

## UI review log (ui-review-loop, 2026-10-09)

External fresh-eyes reviewer over a fixed screenshot tour (`tools/ui_review/`:
`run_tour.sh` wipes + reseeds state through the app's own code paths, walks
15 labeled states with playwright-core + system chromium, `review.sh`
submits to the headless reviewer). Same tour every round; verdict ladder
GA: NO → ACCEPTABLE → GA: YES.

| Round | Findings | Verdict |
|---|---|---|
| 1 | 41 | GA: NO |
| 2 | 12 | ACCEPTABLE |
| 3 | 8 | ACCEPTABLE |
| 4 | 5 | ACCEPTABLE |
| 5 | 3 (all nit) | **GA: YES** |

Round 1 paid for the whole loop before the reviewer saw a frame: driving the
upload through a real browser exposed that htmx matches `responseHandling`
codes as **regexes** — the `"2xx"` notation from the design doc never matches
status 201, so **no successful upload ever swapped its outcome card in a
real browser** (curl-level tests were all green; the fragment was correct,
the swap was dead). Fixed with `"[23].."`-style codes; row deletes (204 +
`hx-swap="delete"`) verified in-browser too.

Biggest fixes across rounds: account sub-header (counts, chain) moved inside
the swap region (stale-count blocker); `chain ✓ (-1/-1 links)` on
zero-parsed accounts eliminated; the run view's terminal state ends in an
action (parser saved → N statements parsed → go to account), never a
contradictory pair of banners; PDF auto-fits the pane so amounts stay
readable; timeline marks the inspected round; launch/pin are anchored
popovers with Close; the JS terminal handler no longer stomps semantic
status colors.

One attempted disprove taught the rule again: the reviewer flagged registry
numeric columns as left-aligned, the CSS said `.num { text-align: right }` —
the DOM said left, because `.table td` outranked `.num`. Verified in the DOM
before ruling, fixed with `.table td.num`.

Keep-list (deliberate, do not re-flag): bare locale-neutral amounts (no
currency field in the canonical dict — a guessed symbol would lie);
registry rows informational only; no token/cost metrics (the harness does
not account tokens — duration shown); draft-code pane plain text; hex
statement id as small metadata; popovers overlay content below them;
right-aligned em-dash placeholders in numeric columns.

Residuals (nit, post-verdict polish applied without a re-round): action
button casing unified to lowercase; launch-form buttons on their own footer
row. Period-column placeholder alignment left as-is (textual column).

A local, single-user web UI over the existing `statementledger` library: users
upload statement PDFs into **accounts** (one folder per account on disk),
statements are parsed with the **registry** (same bank → same parser, zero
LLM), and hard verification failures surface in the UI with a path to
registering a new parser — including a live, watchable distill-harness run
with a PDF overlay showing what the harness is looking at.

Everything below is grounded in the real code; API citations use
`file:line` against the repo root.

---

## 1. Goals and non-goals

**Goals**

- Accounts as first-class objects; one account = one folder; the folder is
  the source of truth (no database).
- Upload a PDF → parsed, *verified* ledger with zero manual config when the
  bank is already registered; the account remembers its registry key.
- Verification failures are first-class UI citizens, not stack traces: the
  per-parser attempts, the exact reconcile/schema errors, and the CLI path
  to a new parser.
- Harness runs (new bank) are launchable from the UI, observable live
  (round timeline + PDF overlay), cancellable, and survive reloads.
- Ethos-compatible: Python-only, local, tiny dependency footprint, the
  wheel still ships only `statementledger` + registry.

**Non-goals (deliberately)**

- No database, no migrations — the filesystem is the store.
- No auth/multi-user in MVP (localhost-bound; see §10).
- No editing of parsed data — parsed output is derived truth; correctness
  comes from the arithmetic gate, not from human fixes.
- No in-browser PDF parsing and no LLM calls from the web process except
  via the harness subprocess (§8).
- No JS framework / npm toolchain.

---

## 2. Mental model and storage

Three concepts map onto disk 1:1:

- **Account** — a real-world bank account the user holds. Belongs to
  exactly one folder. Many accounts may share one registry key (same bank,
  two accounts).
- **Statement** — one uploaded PDF plus its parse outcome. Identified by a
  content hash, so re-uploads dedupe for free and the raw bytes are never
  rewritten.
- **Registry parser** — existing concept, unchanged: a key
  `cc__bank__product__n` and a pure `parse()` module in
  `src/statementledger/registry/`. Parsers are *code* shared by all
  accounts; accounts are *data*.

### 2.1 Storage tree

```
data/                                   # gitignored (add "data/" to .gitignore)
├── accounts/
│   ├── bluelantern-savings/            # one folder per account (slug)
│   │   ├── account.json                # account metadata + registry key pin
│   │   └── statements/
│   │       ├── 3f9c1a2b4d5e6f70/       # statement id = sha256(pdf)[:16]
│   │       │   ├── original.pdf        # raw upload, byte-for-byte, immutable
│   │       │   ├── statement.json      # canonical dict from sl.parse() (+_key)
│   │       │   └── meta.json           # upload/parse metadata, attempts, history
│   │       └── 9d2e08a1c3b4d5e6/
│   │           ├── original.pdf
│   │           ├── statement.json      # absent until first successful parse
│   │           └── meta.json
│   └── copperkettle-checking/
│       ├── account.json
│       └── statements/…
├── runs/                               # distill-harness run tracking (§8–§9)
│   └── 20261009-141023-a1b2/
│       ├── run.json                    # argv, account, statement ids, pid, status
│       └── events.jsonl                # trajectory events (§9.1), copied from the run
├── registry/                           # USER REGISTRY overlay: parsers distilled
│   └── au__bank__product__1.py         # at runtime are USER DATA — saved here by
│                                       # the harness, shadowing same-key built-ins;
│                                       # never committed, never in package source
└── cache/
    └── pages/<sha-prefix>/page_0.png   # lazily rasterized PDF pages (§9.3)
```

The harness's own working directory stays where it is today:
`logs/distill_<YYYYMMDD_HHMMSS>_<stem>/` (`tools/distill_harness.py:1027-1029`),
referenced from `run.json`. `logs/` is already gitignored.

### 2.2 `account.json`

```json
{
  "slug": "bluelantern-savings",
  "display_name": "Blue Lantern Savings",
  "registry_key": "us__bluelantern__savings__1",
  "registry_key_source": "auto-detected",   // "user-pinned" | "auto-detected" | null
  "created_at": "2026-10-09T10:00:00+00:00",
  "notes": ""
}
```

- `registry_key` is the *pin*: when set, uploads parse with exactly this
  parser (`sl.parse(pdf, key=…)`); when null, the account is in detection
  mode (try all parsers, §7).
- Two accounts may pin the same key — the registry is shared code.
- Unpinning (set to null) returns the account to detection mode.

### 2.3 statement `meta.json`

```json
{
  "id": "3f9c1a2b4d5e6f70",
  "sha256": "3f9c1a2b4d5e6f70…",
  "original_filename": "stmt_jan.pdf",
  "size_bytes": 84213,
  "uploaded_at": "2026-10-02T09:14:00+00:00",
  "status": "parsed",                    // "unparsed" | "parsed" | "failed"
  "period": {"start": "2026-01-01", "end": "2026-01-31"},   // null when unknown
  "summary": {
    "transactions": 47,
    "opening_balance": 50000.0,
    "closing_balance": 11663.82
  },
  "history": [                           // append-only; re-parses never erase
    {"at": "2026-10-02T09:14:03+00:00", "outcome": "failed",
     "attempts": [
       {"key": "us__copperkettle__checking__1", "error": "reconcile: row 4 (…): running … vs printed …"},
       {"key": "us__bluelantern__savings__1",  "error": ""}
     ]},
    {"at": "2026-10-02T09:15:10+00:00", "outcome": "parsed",
     "key": "us__bluelantern__savings__1", "statementledger": "0.1.0"}
  ]
}
```

Design points:

- **Duplicate detection** is the id: same sha256 already present in the
  account → 409 with a pointer to the existing statement. The same PDF in a
  *different* account is allowed (folders stay self-contained).
- **Re-parses after a parser upgrade**: `statement.json` is overwritten in
  place (it is derived data); each pass appends one `history` entry with the
  key and `statementledger.__version__`. A registry *version bump* is a new
  key (`us__bank__product__2`), so upgrading an account = re-pin + re-parse
  all (§7.4); same-key module edits (git pull) are covered by the same
  "re-parse all" action.
- **Period** is derived from transaction dates (min/max of non-null dates;
  rows may legitimately carry `date: null` — `validate()` allows it,
  `engine.py:317-321`). Used for overlap/chain checks (§10).

---

## 3. The real API the UI sits on

All of this exists today; the web layer is a thin, honest wrapper around it.

### 3.1 Parsing with / without a key

```python
def parse(pdf: str, key: str | None = None) -> dict
```

`src/statementledger/__init__.py:55-77`.

- `key` given → only that parser is tried.
- `key=None` → every registered key is tried; **the first that verifies
  wins** (candidates come from `keys()`, sorted — `__init__.py:65`).
- Success returns the canonical statement dict tagged with `stmt["_key"]`
  (`__init__.py:71-72`).
- Failure raises `VerificationError` whose message is
  `no registered parser verified <pdf.name>:` followed by one line per
  candidate attempt, `"<key>: <error>"` (`__init__.py:74-76`). Attempt
  errors are one of: `"TypeName: msg | HINT: …"` (crash + remedial hint),
  `"schema: …"` (validate), `"reconcile: …"` (arithmetic divergence).

Note for the backend: don't string-parse that exception. For anything the
UI shows per-parser, use the structured path below (§3.4).

### 3.2 The canonical statement dict

Guaranteed by `validate()` (`engine.py:244-327`), fields per
`CANONICAL_FIELDS` (`engine.py:239-240`):

```json
{
  "bank_name": "Blue Lantern Savings Bank",     // non-empty str
  "account_number": "1234 5678",                // non-empty str
  "opening_balance": 50000.0,                   // number
  "closing_balance": 11663.82,                  // number
  "transactions": [                             // non-empty list
    {"date": "2026-01-02",                      // ISO str or null
     "description": "Opening deposit",           // str
     "amount": 10000.0,                          // number (signed)
     "balance": 60000.0}                         // number or null
  ],
  "totals": [                                    // optional
    {"label": "Total deposits", "kind": "positive", "value": 12345.67}
  ],
  "_key": "us__bluelantern__savings__1"          // added by sl.parse
}
```

Extra keys are allowed by `validate()` (it checks presence/types of the
canonical fields, not absence of others) — this is what makes the
provenance convention of §9.5 possible without touching the engine.

### 3.3 Verification surfaces

- `sl.parse()` only ever returns *reconciled* output — verification is the
  gate, not a post-check (`__init__.py:67-73`).
- `validate(stmt) -> list[str]` — schema errors, empty = valid
  (`engine.py:244`).
- `reconcile_statement(stmt, tol=TOL) -> str | None` — None when it
  reconciles, else the first divergence with row/drift detail
  (`engine.py:367-417`). Cheap (pure arithmetic); powers a "verify again"
  badge on statement detail without re-parsing.
- `sl.VerificationError` — `engine.py:455-456`.
- `sl.hint_for(message) -> str` — `engine.py:330`; already embedded in
  crash attempt errors.

### 3.4 Registry enumeration & structured verdicts (the probe building blocks)

- `sl.keys() -> list[str]` — all registered keys, sorted
  (`src/statementledger/registry.py:22-24`; globs live, every call).
- `sl.load_module(key)` — imports `registry/<key>.py` fresh and checks the
  `KEY` + `parse()` contract (`registry.py:27-42`). Imported into the
  package at `__init__.py:39` (not in `__all__`, still `sl.load_module`).
  **Execs the file every call — no import caching**, so a parser saved by
  the distill harness becomes usable by a running web server with no
  restart. Same for `keys()`.
- `sl.frames_for(pdfs: list[Path], workdir: Path) -> dict` — PDF → frames
  (`src/statementledger/frames.py:179-184`); returns
  `{"statements": [{"name", "lines": [{"page", "y", "items":
  [[text, x1, x2], …]}, …]}, …]}`. `sl.parse` already wraps this in a
  tempdir (`__init__.py:62-64`) — the web layer never manages frames
  itself.
- `sl.evaluate(parse_fn, statements, key=None) -> dict` — the verdict
  (`engine.py:420-452`):

```json
{"key": "us__bluelantern__savings__1", "stage": "parse",
 "results": [{"name": "stmt.pdf", "ok": true, "rows": 47, "error": ""}],
 "clean": 1, "verified": true}
```

The **bank-detection probe** endpoint is exactly what `parse()` does
internally, unrolled so the UI gets structure instead of an exception
string: extract frames once with `frames_for`, then for each key from
`keys()` call `evaluate(module.parse, statements)` — per-key
`{ok, rows, error}` for every candidate.

### 3.5 CLI (for copy-paste instructions)

`statementledger <pdf> [--key KEY]` (`src/statementledger/cli.py:12-22`).
Distill harness: `tools/distill_harness.py <pdfs…> [--key K] [--rounds N]
[--max-probes N] [--no-save]` (`tools/distill_harness.py:993-1025`), env
`STATEMENTLEDGER_LLM_URL/MODEL/API_KEY/PROVIDER/REASONING/ROUNDS/TIMEOUT`
(`distill_harness.py:55-57,380-390`, `.env.example`).

---

## 4. Backend: stack recommendation

**Recommended: FastAPI + Jinja2 server-rendered pages + htmx (vendored as
static files), one process, localhost-bound.**

| Choice | Why |
|---|---|
| FastAPI | Multipart uploads and form parsing for free (via `python-multipart`), typed routes, `BackgroundTasks` if ever needed, async support for the SSE stream (§9.4) without a second framework. The pragmatic default; small surface used. |
| Jinja2 templates + htmx | Server-rendered HTML fragments (htmx swaps them in). No build step, no npm, one vendored `htmx.min.js` (+ `htmx-sse` extension for the run view). Interaction level of this app (upload, buttons, one live stream) is exactly htmx's sweet spot. |
| Sync route handlers for parse (def, not async def) | `pdfplumber` is blocking; FastAPI runs sync handlers in a threadpool — no event-loop stalls. |
| Single uvicorn worker, bound to 127.0.0.1 | Single user; a per-account `threading.Lock` serializes parse-and-write per account (§10). |

**Where it lives and how it stays out of the wheel**: `web/` at the repo
root (`web/app.py`, `web/templates/`, `web/static/`). `pyproject.toml`
packages only `src/statementledger` (`pyproject.toml:17-18`), so `uv build`
is unchanged. Dependencies go in a dedicated group:

```toml
[dependency-groups]
web = ["fastapi>=0.115", "uvicorn>=0.30", "jinja2>=3.1",
       "python-multipart>=0.0.9", "pypdfium2>=4"]
```

(`pypdfium2` is only needed from Phase 3 — page rasterization, §9.3.)
Core runtime deps remain exactly `pdfplumber` (`pyproject.toml:6-8`);
default `uv sync` environments are unchanged. Run:

```console
$ uv sync --group web
$ uv run --group web uvicorn web.app:app --host 127.0.0.1 --port 8765
```

**Alternatives weighed**

- *Starlette alone* (no pydantic): valid slim-down, saves two transitive
  deps; costs hand-rolled route ergonomics. Acceptable variant, not
  recommended — the win is marginal and FastAPI is the better-known
  default for the occasional contributor.
- *Flask + htmx*: equally lean in spirit; weaker fit for the SSE endpoint
  and multipart+JSON dual responses. Not chosen.
- *FastAPI + vanilla-JS or SPA frontend*: rejected. A JS frontend (even
  hand-rolled fetch code) buys nothing over htmx here and drags in a build
  toolchain; the ethos is "one runtime dep, Python-only, local".

**Response convention**: routes return HTML fragments for htmx by default
and JSON when the request `Accept`s it (exports, `*.json` paths). Protocol
errors use HTTP status codes (400/404/409/413); *domain* outcomes —
"parsed", "no parser verified" — are 200-level responses with a `status`
field in the body, because a failed parse of a successfully stored upload
is not a transport error, and htmx swaps the failure fragment in like any
other outcome.

---

## 5. HTTP API

| Method | Path | Request | Success response | Errors |
|---|---|---|---|---|
| GET | `/` | — | HTML: accounts list | — |
| POST | `/accounts` | form: `display_name`, `registry_key?` | 303 → `/accounts/{slug}` | 400 empty/invalid name; 409 slug exists |
| GET | `/accounts/{slug}` | — | HTML: account detail (statements, chain status, upload zone, active runs) | 404 |
| DELETE | `/accounts/{slug}` | — (confirm dialog) | 204 | 404 |
| POST | `/accounts/{slug}/statements` | multipart: `file` (PDF) | 201 + statement fragment (parsed, failed, or duplicate-notice per §7); stores PDF and meta first, parse outcome in body | 400 not a PDF / empty; 413 > size limit; 404 account |
| POST | `/accounts/{slug}/statements/{id}/parse` | — (uses account pin; body may override `key`) | 200 + `{status: "parsed", summary, period}` or `{status: "failed", attempts: [{key, error}]}` | 404 |
| GET | `/accounts/{slug}/statements/{id}` | — | HTML: statement detail (§6.5) | 404 |
| GET | `/accounts/{slug}/statements/{id}/ledger.json` | — | canonical statement dict (§3.2) | 404; 409 not parsed yet |
| GET | `/accounts/{slug}/statements/{id}/ledger.csv` | — | CSV of transactions | 404; 409 |
| GET | `/accounts/{slug}/ledger.json` | — | merged ledger across parsed statements (v2) | 404 |
| DELETE | `/accounts/{slug}/statements/{id}` | — | 204 (removes the statement folder) | 404 |
| POST | `/accounts/{slug}/key` | form: `registry_key` (empty = unpin) | 303 → account page | 400 key not in `sl.keys()`; 404 |
| POST | `/accounts/{slug}/probe` | multipart `file` **or** JSON `{statement_id}` | 200 `{results: [{key, ok, rows, error}], verified_count, suggested_key}` — nothing persisted | 400/404/413 as upload |
| GET | `/registry` | — | HTML: parser table (key, description, statements using it) | — |
| GET | `/registry.json` | — | `[{key, description}]` from `sl.keys()` + module docstring first line via `sl.load_module` | — |
| POST | `/runs` | JSON `{account_slug, statement_ids: […], mode: "draft"\|"maintain", key?, rounds?, max_probes?}` | 202 `{run_id}`; spawns the harness (§8.2) | 400 bad payload/key; 404; 409 a run is already active |
| GET | `/runs` | — | HTML/JSON: active + recent runs with status | — |
| GET | `/runs/{id}` | — | `run.json` state (status, round count, key on success, workdir path) | 404 |
| GET | `/runs/{id}/events` | `?after=<seq>` (reconnect) | `text/event-stream`: SSE of trajectory events (§9.1) | 404 |
| DELETE | `/runs/{id}` | — | 202 (cancelling; terminal event `cancelled`) | 404 |
| GET | `/runs/{id}/rounds/{n}` | — | `{round, kind: "probe"\|"script", code, verdict?}` — `round_N.py` text + gate verdict for the code pane (§9.4) | 404 |
| GET | `/accounts/{slug}/statements/{id}/pages/{n}.png` | — | `image/png` (lazily rasterized, cached under `data/cache/pages/`) | 404 |

`GET /runs/{id}/events` keeps SSE semantics: one event per trajectory line,
`id:` = sequence number so reconnects with `?after=` resume without loss.

---

## 6. Pages and wireframes

### 6.1 Accounts list — `/`

```
┌────────────────────────────────────────────────────────────────────────┐
│ statementledger                             [registry: 2 parsers] [+ New account] │
├────────────────────────────────────────────────────────────────────────┤
│ ACCOUNTS (2)                                                           │
│                                                                        │
│ ┌────────────────────────────────────────────────────────────────────┐ │
│ │ Blue Lantern Savings                     ◇ us__bluelantern__savings__1│ │
│ │ 12 statements · Jan 2026 → Dec 2026 · chain ✓ (11/11 links)        │ │
│ │ latest closing 11,663.82 · updated 2026-10-02               [Open] │ │
│ └────────────────────────────────────────────────────────────────────┘ │
│ ┌────────────────────────────────────────────────────────────────────┐ │
│ │ Copper Kettle Checking                   ◇ us__copperkettle__checking_1│ │
│ │ 8 statements · … · ⚠ chain break: Jun→Jul opening mismatch        │ │
│ │ latest closing 14,972.32 · 1 statement needs attention    [Open]  │ │
│ └────────────────────────────────────────────────────────────────────┘ │
│                                                                        │
│ ┌ + New account ────────────────────────────────────────────────────┐ │
│ │ Display name [___________________]  Parser (optional) [▾ none ▾]  │ │
│ │                                            [Cancel] [Create]      │ │
│ └───────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────┘
```

### 6.2 Account detail — `/accounts/{slug}`

```
┌─ Blue Lantern Savings ────────────── ◇ pinned: us__bluelantern__savings__1 [change] ─┐
│ account ····5678 (from latest statement) · 12 statements · chain ✓ (11/11 links)      │
│                                                                                       │
│ ┌───────────────────────────────────────────────────────────────────────────────────┐ │
│ │  ⬆ Upload statement PDF — drag & drop or click · PDF only · ≤ 25 MB                │ │
│ └───────────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                       │
│ PERIOD     TXNS  OPENING → CLOSING         STATUS                  ACTIONS             │
│ 2026-01    47    50,000.00 → 11,663.82    ✓ parsed               view · reparse · ✕  │
│ 2026-02    39    11,663.82 → 9,210.05     ✓ parsed               view · reparse · ✕  │
│ 2026-03    41    9,210.05 → 12,004.10     ✓ parsed  ⚠ overlap    view · reparse · ✕  │
│            ↑ overlaps 2026-02 by 3 days (warn only)                                   │
│ 2026-09     —     —                        ✗ no parser verified    view · [Register new bank…] │
│ 2026-10     —     —                        ◻ unparsed (queued)    parse · ✕          │
│                                                                                       │
│ DISTILL RUNS                                                                          │
│ ▶ 2026-10-09 14:10 · draft · round 7/30 · gate ✗ reconcile row 12   [Watch live] [✕]  │
│ ✓ 2026-10-08 23:57 · maintain → us__copperkettle__checking__1 v1    [View run]        │
│                                        [ Re-parse all with ◇ pinned key ]             │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

The "Register new bank…" button on a failed statement opens the launch
dialog (§6.4): mode draft (no key) or maintain (existing key), round
budget, model name from env, cost/latency warning, [Launch run] → run view.

### 6.3 Upload outcome states (swap into the statements table / toast)

```
(a) PARSED                                        (b) DUPLICATE
┌───────────────────────────────────────┐         ┌────────────────────────────────┐
│ ✓ 2026-03 · 41 transactions           │         │ ⚠ Already uploaded             │
│   parsed with us__bluelantern__…__1   │         │   same file (sha256) as 2026-03│
│   reconciled ✓  opening 9,210.05 →    │         │   uploaded 2026-10-01          │
│   closing 12,004.10   [view]          │         │                [view existing]  │
└───────────────────────────────────────┘         └────────────────────────────────┘

(c) NO PARSER VERIFIED (new bank)
┌───────────────────────────────────────────────────────────────────────────────┐
│ ✗ No registered parser verified stmt_sep.pdf                                  │
│   Every attempt failed the arithmetic gate (opening + amounts ≠ closing, or    │
│   schema violations). Attempts:                                                │
│   ┌──────────────────────────────────────────────┬──────────────────────────┐ │
│   │ us__copperkettle__checking__1                 │ reconcile: row 4 (…):    │ │
│   │                                               │ running 24,868.94 vs …   │ │
│   │ us__bluelantern__savings__1                   │ schema: transactions[3]… │ │
│   └──────────────────────────────────────────────┴──────────────────────────┘ │
│   The PDF is stored; nothing was parsed.                                       │
│                                                                               │
│   [ Register new bank for this statement… ]   ← launches a distill run (§6.4)  │
│                                                                               │
│   …or run it yourself:                                                         │
│   $ uv run python tools/distill_harness.py \                                   │
│         data/accounts/bluelantern-savings/statements/3f9c…/original.pdf \      │
│         --max-probes 8                                                         │
│   (needs STATEMENTLEDGER_LLM_* in .env — see .env.example)                     │
│   [ Re-probe parsers ]  ← after a run saves a new parser; keys() picks it up   │
│                                   live, no server restart                      │
└───────────────────────────────────────────────────────────────────────────────┘
```

### 6.4 Distill run view — `/runs/{id}` (split pane, §9)

```
┌─ Run 2026-10-09 14:10 · draft · Blue Lantern Savings · round 7/30 ──── [Cancel run] ─┐
│ ┌─────────────────────────────── PDF ────────────────────────┬─ TIMELINE ─────────┐│
│ │  page 1 of 1                          [50%] [fit] [100%]   │ ▸ r0  probe  ✓ 0.4s││
│ │  ┌─────────────────────────────────────────────────────┐  │ │  looking at top   ││
│ │  │  Copper Kettle Bank                                 │  │ ▸ r1  probe  ✓ 0.6s││
│ │  │  Synthetic Monthly Statement                        │  │ │  looking at tail   ││
│ │  │ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ ← overlay    │ ▸ r2  script gate ✗   ││
│ │  │ ▓ Statement Period:  1 Feb 2026…      ▓▓▓▓▓▓▓▓▓     │  │ │  schema: transact… ││
│ │  │ ▓ Opening balance:   $25,000.00 CR    ▓▓▓▓▓▓▓▓▓     │  │ ▸ r3  script gate ✗   ││
│ │  │ ▓ Closing balance:   $14,972.32 CR    ▓▓▓▓▓▓▓▓▓     │  │ │  reconcile: row 12  ││
│ │  │  Date  Description  Debit  Credit  Balance           │  │ ▸ r4  probe  ✓ 0.5s  ││
│ │  │ ▓▓ 01 Feb deposit … 131.06 … 24,868.94 CR ▓▓▓▓      │  │ │  looking at rows    ││
│ │  │ ▓▓ 03 Feb Rent payment …              ▓▓▓▓▓▓▓      │  │ ▸ r5  script gate ✗   ││
│ │  └─────────────────────────────────────────────────────┘  │ ▸ r6  script gate ✗   ││
│ │  selection: r4 · "transaction rows" · lines 8–35          │ ▸ r7  script gate … ⏳ ││
│ │  (auto-follow latest ●)  (scrub: click any round ⇆)       │ ├────────────────────┤│
│ │                                                           │ │ DRAFT round_7.py   ││
│ │                                                           │ │ KEY = "us__newba…" ││
│ │                                                           │ │ def parse(frames): ││
│ │                                                           │ │   lines = frames[…  ││
│ │                                                           │ │ …    [full code]   ││
│ └───────────────────────────────────────────────────────────┴──────────────────────┘│
│ status: gate round 7 — reconcile: row 12 (‘Rent payment’): running 24,497.32 vs      │
│ printed 24,868.94 (amount −371.62)   ·   model: gpt-6-luna · started 14:10:23        │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### 6.5 Statement detail — `/accounts/{slug}/statements/{id}`

```
┌─ 3f9c1a2b · Blue Lantern Savings · Jan 2026 ───────────────── [back to account] ─┐
│ ✓ RECONCILED — verified by the arithmetic gate at parse time                     │
│   key us__bluelantern__savings__1 · statementledger 0.1.0 · parsed 2026-10-02    │
│   [verify again] → reconcile: OK (opening 50,000.00 -> closing 11,663.82)        │
│   opening 50,000.00 · closing 11,663.82 · 47 transactions · totals ✓ (2/2)       │
│   original.pdf (84 KB) [open] · export [JSON] [CSV] · [re-parse] [delete]        │
│ ┌─────────────────────────────────────────────────────────────────────────────┐ │
│ │ DATE        DESCRIPTION                AMOUNT        BALANCE                │ │
│ │ 2026-01-02  Opening deposit         + 10,000.00   60,000.00                │ │
│ │ 2026-01-05  Grocery run                -    94.25  59,905.75               │ │
│ │ …                                                                           │ │
│ │ (row hover from Phase 3: highlight the source line on the rendered page —    │ │
│ │  same overlay component as the run view, §9.5)                              │ │
│ └─────────────────────────────────────────────────────────────────────────────┘ │
│ PARSE HISTORY                                                                     │
│ 2026-10-02 09:15 parsed with us__bluelantern__savings__1 (47 txns)               │
│ 2026-10-02 09:14 failed — 2 attempts (details collapsed)                         │
└──────────────────────────────────────────────────────────────────────────────────┘
```

---

## 7. Upload and parse flow

### 7.1 Happy path (pinned account)

1. `POST /accounts/{slug}/statements` receives the multipart file.
2. Hygiene gate (§10): magic bytes, size, non-empty.
3. `sha256` → id; if the statement folder exists → 409 duplicate fragment.
4. Store `original.pdf` + `meta.json` (`status: "unparsed"`). **The upload
   is now durable; parse failures can never lose the file.**
5. Parse synchronously with the pin: `sl.parse(str(pdf), key=account_key)`
   (§3.1). Success → write `statement.json`, set `status: "parsed"`,
   derive period/summary, append history. Response: fragment (6.3a).
6. Failure (the pinned parser stopped fitting, e.g. a layout change):
   record attempts (structured, via the probe path §7.3 unrolled for the
   single key), `status: "failed"`, response fragment offers re-detection,
   maintain-mode run, or CLI.

### 7.2 Unpinned account (first upload) — detection

1. Steps 1–4 as above.
2. Extract frames **once** (`sl.frames_for([pdf], tmpdir)`), then for each
   key in `sl.keys()` load the module (`sl.load_module`) and run
   `sl.evaluate(module.parse, statements)` — the structured unrolling of
   `sl.parse`'s loop (`__init__.py:65-74`).
3. Exactly one verifies → parse with that key, save, and **suggest pinning**
   the account to it (one click; `registry_key_source: "auto-detected"`).
4. More than one verifies → surface the candidates and let the user pick
   which to pin (never auto-pin on ambiguity; `sl.parse`'s first-wins rule
   is fine for a one-off parse, not for a permanent account association).
5. None verifies → `status: "failed"`, `attempts` persisted in meta;
   UI state 6.3(c): per-parser failure table + "Register new bank…" +
   copy-paste CLI + "Re-probe parsers" (new registry modules appear in
   `sl.keys()` live — no restart, §3.4).

### 7.3 Probe without persistence

`POST /accounts/{slug}/probe` answers "what would happen?" for a file (or
an already-stored statement) without saving anything: same frames-once +
evaluate-per-key loop, results returned, nothing written. Used by the
"Re-probe" button and as the dry-run behind detection.

### 7.4 Re-parse (parser upgraded)

- Per statement: `POST …/{id}/parse` — same code path as upload-parse;
   overwrites `statement.json`, appends history.
- Per account: "Re-parse all with pinned key" — sequential per-statement
  parse; report shows old→new (txn count, opening/closing, period) per
  statement; any drift is flagged loudly (the gate should prevent silent
  divergence, but two verified parses can still differ in what they read —
  e.g. a totals[] check added in v2).
- A *version bump* (new key `…__2`) is: pin the account to the new key
  (confirm dialog), then re-parse all. Old history entries keep the old
  key, so provenance is auditable.

---

## 8. Job model

Two very different workloads; treat them differently.

### 8.1 Statement parsing: synchronous

Frames extraction via pdfplumber is seconds-fast for multi-page personal
statements and `evaluate` is milliseconds; the user is one person on
localhost. **Parse runs inside the request** (upload response already
carries the outcome), with a generous route timeout (~60 s). No queue, no
worker, no job table — a failed parse is a `status` field, not a 500.
If monthly batch upload (drag 12 PDFs) arrives later, FastAPI
`BackgroundTasks` + htmx polling is the minimal escalation; deliberately
not built now.

Upload and parse are **separate endpoints** so that (a) a stored PDF can
be re-parsed later (same code path), and (b) parse failure never loses the
upload — but the upload handler *calls* the parse logic inline so the
first response already has the result.

### 8.2 Distill harness runs: the one real background job

The harness is minutes-long (default 30 LLM rounds), costs money, needs
`STATEMENTLEDGER_LLM_*` secrets, and runs sandboxed children — it must be
a tracked, cancellable subprocess, never inline in a request.

**Recommendation: (a) the web backend spawns the harness as a tracked
subprocess with a small, optional `--jsonl-trajectory <path>` flag.**

- Spawn: `uv run python tools/distill_harness.py <stored original.pdf
  paths…> [--key K] [--rounds N] [--max-probes N] --jsonl-trajectory
  data/runs/<id>/events.jsonl` — cwd = repo root, env inherited *plus*
  `.env` loaded via `tools/envloader.py` (the key never reaches the
  browser; the server process itself doesn't need the env at boot).
- The harness already emits everything needed as data: per-round artifacts
  `round_N.py` / `round_N.out` / `gate_round_N.{py,out}`, an append-only
  flushed `trace.log` (`distill_harness.py:596-615`), structured gate
  verdicts as JSON (`GATE:{…}` from `evaluate`, `distill_harness.py:260-272,
  275-303`), and a per-round `history` list (`draft_by_repl` returns it,
  `distill_harness.py:688-692`). The minimal patch is an `emit(type, **fields)`
  helper that appends one JSON line at the existing `trace.log` call sites
  (~10 call sites in `main`/`draft_by_repl`/`py_gate`) — no behavior
  change, ~30 lines, flag-gated so the CLI is unchanged without it.
- The backend tails `events.jsonl` (plus the process exit status) and
  re-emits it over SSE. `run.json` records argv, pid, account, statement
  ids, status, timestamps — reload-safe (§8.3).

**Fallback (b), zero harness change**: the backend spawns the subprocess
as-is and **tail-watches** the run directory (`logs/distill_*` path
returned/predictable from spawn time; watch for new `round_N.py`,
`round_N.out`, `gate_round_N.out`, `trace.log` growth) and *derives*
trajectory events from artifacts — gate verdicts are parseable
(`GATE:{json}` line), probe-vs-script is the `# probe` first-line
convention (`distill_harness.py:773`), lint/chat failures from
`trace.log` sections. Works today, but it is parsing prose; it cannot
carry live selection events (§9.2), which is exactly the "super cool"
part. Also usable read-only: point the UI at a run started manually in a
terminal ("attach to existing run" — pass the `logs/distill_*` path).

Decision: **(a) is shipped (2026-10-10), always-on rather than
flag-gated**: the agent's `Trace` writes `events.jsonl` beside `trace.log`
at the same call sites (`tools/distill_agent.py` Trace.emit), and the run
manager tails it verbatim. **(b) remains as the attach-mode/legacy
fallback** — a workdir with only a `trace.log` (pre-emission agent) is
derived through `web/agent_events.py:TraceDeriver`, same event vocabulary.
The harness stays a plain CLI tool; the emitted file is just another
artifact in its workdir.

**Lifecycle**: one active run at a time (409 otherwise — bound the LLM
spend); `DELETE /runs/{id}` sends SIGTERM to the process group, then
SIGKILL after a grace period; terminal status (`converged` with saved key
/ `failed` / `cancelled`) is persisted in `run.json` and re-read on
server restart; a run whose pid is gone on restart is marked `orphaned`
with its artifacts still browsable. On `converged`, the backend
auto-parses the pending statement(s) that triggered the run with the new
key and offers to pin the account (`registry_key_source:
"auto-detected"`); registry pickup needs no restart (§3.4).

---

## 9. Harness run view: trajectory + PDF overlay

### 9.1 Trajectory event schema (NDJSON, one object per line)

Emitted by the patched harness (`--jsonl-trajectory`), relayed verbatim as
SSE by the backend (`id:` = line number):

```json
{"type": "run_started",  "ts": "…", "pdfs": ["…"], "mode": "draft", "model": "…", "rounds": 30}
{"type": "frames",       "ts": "…", "statements": [{"name": "…", "pages": 1, "lines": 43}]}
{"type": "round_started","ts": "…", "round": 0}
{"type": "model_output", "round": 0, "kind": "probe", "chars": 214}
{"type": "round_saved",  "round": 0, "kind": "probe", "path": "round_0.py"}
{"type": "probe_select", "round": 0, "page": 0, "line_ids": [0,1,2,3,4,5,6,7],
 "rect": [72, 60, 503, 132], "label": "statement header + table top"}
{"type": "round_ran",    "round": 0, "returncode": 0, "elapsed": 0.4,
 "stdout_tail": "…[Page 0]…"}
{"type": "gate",         "round": 1, "verdict": {"key": null, "stage": "parse",
 "clean": 0, "verified": false, "results": [{"name": "stmt.pdf", "ok": false,
 "rows": 0, "error": "schema: transactions[3]: amount must be a number…"}]}}
{"type": "lint_rejected","round": 2, "violation": "transcribes an amount literal"}
{"type": "chat_failed",  "round": 3, "error": "…"}
{"type": "feedback_sent","round": 1, "summary": "reconcile: row 12 …"}
{"type": "converged",    "key": "us__newbank__checking__1", "path": "verified.py", "saved": true}
{"type": "failed",       "reason": "rounds exhausted (best: 0/1 statements clean)"}
{"type": "cancelled",    "ts": "…"}
```

Every event type corresponds to an existing code site (round loop,
`lint_code`, `py_gate`, `script_feedback`, save at
`distill_harness.py:1136-1137`); unknown types are ignored by the UI —
the schema is additive.

### 9.2 Live selection events (the overlay's data)

Probes run in a sandboxed child; their only channel out is stdout, which
the parent already captures as data (`run_script`, `distill_harness.py:544-590`).
Convention (part of the same small patch): a probe script *may* end by
printing one line

```
SELECT:{"page": 0, "lines": [8, 35], "label": "transaction rows"}
```

The parent recognizes the `SELECT:` prefix, resolves `lines` against
`frames.json` into concrete `line_ids` (and a bounding `rect` in PDF
points), and emits the `probe_select` event. The system prompt's probe
instructions gain one sentence teaching the convention; the `pl.show()`
probes the model already writes can additionally be *approximated* by the
parent: `show(start=S, n=N)` output maps to `line_ids = [S … S+N)` on the
statement's pages — a zero-model-cooperation heuristic that covers most
probes even without the `SELECT:` line. Sandbox integrity is preserved:
selections come out as data on stdout, never as side effects.

**Zero-change fallback** (attach mode): no live selections; the overlay
appears only post-run, on the converged parser, via provenance (§9.5).

### 9.3 Rendering the PDF (and the overlay) — server-side raster

**Recommendation: server-side rasterization with `pypdfium2`**, pages
rendered lazily to PNG (scale ~2.0 ≈ 144 dpi), cached content-addressed
under `data/cache/pages/<sha-prefix>/page_N.png`; the browser just shows
`<img>`/`<canvas>` tags with absolutely-positioned highlight `<div>`s on
top.

- Why not pdf.js in-browser: it is a large vendored JS blob, and overlay
  alignment must match pdf.js's own text-layer transforms — fiddly,
  JS-debug-heavy, against the no-npm ethos. pypdfium2 is a single
  self-contained wheel (bundled pdfium binary), zero system deps, and
  keeps the coordinate math in Python where the frames already live.
  (PyMuPDF rejected: AGPL.)
- **Coordinate mapping** — frames lines carry `{page, y, items:
  [[text, x1, x2], …]}` in pdfplumber points, y measured **downward from
  the top of the page** (`frames.py:17-28`); pdfium raster origin is also
  top-left at natural scale = points. So for render scale `s`:
  `px = x * s`, `py = y * s`, and a line highlight band is
  `rect = [min(x1)*s, (y − H)*s, max(x2)*s, (y + 2)*s]` with a default
  band height `H ≈ 11 pt` (frames store only y — the binned bottom edge —
  not glyph height; one knob, tuned once). Whole-line ids are the stable
  unit: `line_ids` index into the run's `frames.json`, which the backend
  serves next to the page PNGs. Caveat, one line: if a PDF has a non-zero
  cropbox origin or `/Rotate`, add the cropbox offset / rotate the overlay
  (verify once per PDF; the synthetic fixtures have neither).
- UI: 50% / 100% zoom = re-request at different scale or CSS-scale the
  cached image (overlay scales linearly either way).

### 9.4 Run view interaction

Split pane (wireframe §6.4): PDF + overlay left; right column = round
timeline (probe ✓ / gate ✗ with the failing row, gate ✓, lint ✗, chat ✗),
expandable probe stdout tail, and the draft parser code
(`GET /runs/{id}/rounds/{n}` → `round_N.py`). **Click a round to scrub**
the overlay to that round's last selection; while live, **auto-follow**
pins to the newest event (indicator dot; any manual scrub unpins).
Transport: **SSE** (`GET /runs/{id}/events`) — one-way, minutes-long,
localhost; reconnect via `Last-Event-ID`/`?after=`; htmx SSE extension or
a ~20-line `EventSource` shim. Polling (2 s) is the degraded mode if SSE
is ever annoying, but SSE is the right default: it makes the overlay feel
live rather than sampled.

### 9.5 Stretch: provenance overlay on any parsed statement

The same PDF+overlay component, post-run and on any statement detail
page: highlight which printed lines produced the opening/closing anchors
and each transaction row. Mechanism: `validate()` ignores extra keys
(§3.2), so parsers may adopt an *optional* provenance convention — e.g.
`txn["line"] = <index into frames lines>` and `stmt["_anchors"] =
{"opening_balance": 2, "closing_balance": 3}` — roughly five lines per
registry module, adopted incrementally; the UI degrades gracefully
(no convention → no overlay). The run view's post-hoc fallback uses the
same rendering with the converged parser's provenance.

---

## 10. Edge cases and hygiene

- **Upload validation**: reject non-PDF by magic bytes — `%PDF-` within
  the first 1024 bytes (extension untrusted); reject empty files; size
  cap default 25 MiB (env `STMTLEDGER_WEB_MAX_MB`); `original_filename`
  stored in meta but never used on disk (on-disk name is the hex id).
- **Encrypted / scanned PDFs**: pdfplumber raises on encrypted files →
  catch, `status: "failed"` with a plain-language message; frames with
  zero lines → "no extractable text — scanned image PDF?" hint (the UI
  can point at `tools/heal_pdf.py` for damaged-but-text PDFs).
- **Duplicates**: sha256 id (per account). Same PDF re-uploaded → 409 +
  link (state 6.3b). Same PDF in another account → allowed, with an
  informational note (folders stay self-contained).
- **Period overlaps**: derived periods compared pairwise; overlap (or gap)
  between adjacent statements is a *warning chip*, never a block — banks
  do reissue corrected periods.
- **Chain check**: adjacent statements must satisfy
  `prev.closing == next.opening` within `TOL = 0.01` (`engine.py:28`);
  account header shows chain ✓/break with the offending link.
- **Parser version bumps**: new key → confirm dialog → re-pin → re-parse
  all (§7.4); same-key edits → "re-parse all" directly. History keeps
  every past parse auditable.
- **Path safety**: account slugs are server-generated from the display
  name (`[^a-z0-9]+` → `-`, lowercased, trimmed, length-capped), must
  match `^[a-z0-9][a-z0-9-]{0,63}$`, and are additionally checked with
  `Path.resolve().is_relative_to(accounts_root)`. Statement ids are hex
  from sha256 — safe by construction. No user string ever becomes a path
  component.
- **Local-only, no auth — explicit assumption**: the server binds
  `127.0.0.1` by default; there is deliberately no authentication,
  CSRF protection, or multi-user anything. Statements are sensitive
  financial data — the docs (this file + README section when built) must
  say plainly: do not port-forward, do not run on a shared host. If
  exposure is ever needed, a single bearer token + `--host` opt-in is the
  v-later path.
- **Git hygiene**: add `data/` to `.gitignore` next to the existing
  `*.pdf` rule (`.gitignore` already excludes all PDFs and `logs/`;
  `data/` keeps run state and caches out too). Parsed JSON contains real
  transaction data — it must never be committable by accident.
- **Concurrency**: one user, but two tabs can race. A per-account
  `threading.Lock` around parse-and-write; statement writes are
  idempotent (content-addressed). Single uvicorn worker.
- **Cost guard on runs**: launch dialog shows model name (from env) and
  round budget; one active run; cancel always one click away.

---

## 11. Phased plan

**Phase 1 — MVP: upload + parse + browse, hard failures surfaced**
- `web/` app skeleton (FastAPI + Jinja2 + vendored htmx), `[dependency-groups] web`.
- Storage tree, account create/list/detail, statement upload (magic/size/
  dedupe), sync parse (pinned + detection modes), statement detail with
  transactions table and verification banner, failure state 6.3(c) with
  per-parser attempts and copy-paste CLI instructions, `/registry` page,
  `.gitignore` `data/`.
- No: probe endpoint, runs, exports, overlay.

**Phase 2 — detection UX, re-parse, exports**
- `POST …/probe` (dry-run try-all), pin/unpin flow incl. ambiguous-candidate
  picker, "Re-probe parsers" on failed statements, per-statement and
  per-account re-parse with old→new drift report, `ledger.json` /
  `ledger.csv` exports, merged account ledger, chain check + overlap
  warnings on the account page.

**Phase 3 — distill integration: launch + trajectory + overlay**
- `--jsonl-trajectory` patch to `tools/distill_harness.py` (opt-in flag;
  emit at existing trace sites; `SELECT:` probe convention §9.2).
- `POST /runs` spawn/track/cancel, `run.json` persistence, orphan
  handling, one-run lock, auto-parse of pending statements on converge.
- Run view: SSE relay, round timeline, gate badges, draft code pane;
  `pypdfium2` page raster + cache; live selection overlay, scrub,
  auto-follow; attach-to-existing-run (tail-watch fallback).
- Stretch: provenance convention in registry modules + overlay on
  statement detail.

**Later (only if ever needed)** — multi-statement batch upload via
`BackgroundTasks`; bearer-token + non-localhost bind; anything multi-user
(out of scope until asked for).

## 12. React SPA at /app (2026-10-09)

The UI grew a React frontend (`web/frontend`: Vite + React + TS + Tailwind v4 +
shadcn/ui radix-nova/neutral + TanStack Query + react-router, basename `/app`).
It talks to the SAME routes as the Jinja UI via content negotiation — every
page route gained a `wants_json` branch returning the page's data, and FastAPI
serves the built bundle at `/app` with an SPA history fallback (only when
`web/frontend/dist` exists; a node-less machine just loses `/app`). Dev:
`npm run dev` in `web/frontend` proxies `/accounts`, `/runs`, `/registry*` to
:8765. The run view is the flagship screen: the SSE trajectory folds into a
reducer (`src/pages/run/store.ts`) and the PDF overlay, timeline, and
code/trace panes render from state — no imperative DOM patching, so
auto-follow/scrub/code-fetch can't race each other. The Jinja+htmx UI remains
at `/` until the SPA reaches parity trust, then retires.

SPA review loop (same tour discipline as before; 12 states, external
fresh-eyes reviewer, verdict ladder):

- **Round 1** — 19 findings → GA: NO. Fixed 15 (ledger truncation vs closing
  card, Parsed-banner grid stacking, stale `running…` after terminal, bare
  cancel banner, follow-toggle after terminal, tab anchoring, slug breadcrumb,
  debit contrast, KiB sizes, loud danger zone, missing empty-state h1,
  double-boxed dropzone, duplicate CTAs, link affordance). Kept 4 (workspace
  void when few rounds; no currency symbol — schema has none, exports are
  symbol-free; `—` on the numeric axis; docstring-punctuated descriptions).
  Disproved 1 by DOM check (numeric headers are right-aligned).
- **Round 2** — 11 findings → GA: NO. Real misses fixed (terminal rewrite of
  `running…` badge text, not just the pulse flag; cancelled/failed end card
  with icon; "live tail" scoped to the live round; column width hints;
  transactions/PDF caption alignment; destructive color cascading into the
  outline button). Disproved the filename-underline claim via
  `text-decoration` DOM check.
- **Round 3** — 8 findings → ACCEPTABLE. Fixed all (selected-round highlight,
  Accounts nav active on child routes, blue in-flight badges, pinned-failure
  icon + title/body hierarchy, nav/distill icons, mode chip).
- **Round 4** — verified all round-3 fixes; one nit (model name missing while
  live — now folded from the `run_started` event, no refetch) → **GA: YES**.

Tour harness: `tools/ui_review/spa_check.mjs` (playwright walk-through, fails
on console errors); DOM assertions in `check_th.mjs`; prompts
`prompt-spa-r{1..4}.txt`.

**2026-10-10 — Jinja UI retired.** The React SPA is the only UI, served at `/`
(assets under `/assets`, history fallback for unknown browser GETs, JSON 404s
for API clients, legacy `/app/*` links 308 to root). The server is a pure JSON
API that content-negotiates by Accept header — the SPA's fetches and browsers
share the same URLs. `web/templates/` and `web/static/` are deleted; htmx
response handling is gone. Malformed statement ids now 400 instead of leaking
a store ValueError as a 500. Suite: 91 (SPA-shell tests skip when the bundle
isn't built).
