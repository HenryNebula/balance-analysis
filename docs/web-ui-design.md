# statementledger web UI — design

A local, single-user web UI over the existing `statementledger` library: users
upload statement PDFs into **accounts** (one folder per account on disk),
statements are parsed with the **registry** (same bank → same parser, zero
LLM), and hard verification failures surface in the UI with a path to
registering a new parser — including a live, watchable distill-agent run
with a PDF overlay showing what the agent is looking at.

This document describes the UI as shipped: a FastAPI JSON API (`web/app.py`
and friends) serving a React SPA (`web/frontend`). How the design got here —
a first Jinja2+htmx implementation, its retirement, and the review loops
both UIs went through — is compressed into the history appendix at the end.

Everything below is grounded in the real code; API citations use
module-level symbols (`sl.parse` → `statementledger/__init__.py`, etc.).

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
- Agent runs (new bank) are launchable from the UI, observable live
  (round timeline + PDF overlay), cancellable, and survive reloads.
- Ethos-compatible: the web stack stays in a dependency group, the wheel
  still ships only `statementledger` + registry.

**Non-goals (deliberately)**

- No database, no migrations — the filesystem is the store.
- No auth/multi-user (localhost-bound; see §10).
- No editing of parsed data — parsed output is derived truth; correctness
  comes from the arithmetic gate, not from human fixes.
- No in-browser PDF parsing and no LLM calls from the web process except
  via the agent subprocess (§8).

---

## 2. Architecture: a JSON API and its SPA

Two processes' worth of concerns, one process:

- **Server** — FastAPI, single uvicorn worker bound to 127.0.0.1. Parse
  routes are sync handlers (`def`, not `async def`): `pdfplumber` is
  blocking and FastAPI runs sync handlers in a threadpool, so the event
  loop never stalls. A per-account `threading.Lock` serializes
  parse-and-write (§10).
- **UI** — a React SPA (`web/frontend`: Vite + TypeScript + Tailwind,
  TanStack Query, react-router) built to static files. The server serves
  the bundle at `/` (assets under `/assets`, history fallback for unknown
  browser GETs; legacy `/app/*` links 308 to root). **The SPA is the only
  UI; the server is its JSON API**: page routes content-negotiate by
  `Accept` header — browsers (`Accept: text/html`) get the SPA shell so
  deep links and refresh work without hash routing, the app's fetches
  (`Accept: application/json`) get the page's data from the same URLs.
  Without the built bundle the server is API-only (a 503 with build
  instructions points at `tools/serve_web.sh`).

**Response convention**: protocol errors use real HTTP status codes
(400/404/409/413) with JSON bodies; *domain* outcomes — "parsed",
"no parser verified", "duplicate" — are 200-level responses carrying a
`status` field in the body, because a failed parse of a successfully
stored upload is not a transport error.

**Storage location**: everything the app writes lives under
`STMTLEDGER_WEB_DATA` — repo-relative `data/` when unset (gitignored),
`~/.local/share/statementledger` when launched via `tools/serve_web.sh`,
which also builds the SPA on first run. The web dependencies sit in the
`web` dependency group (`uv sync --group web`); `node`/`npm` are needed
only to build the SPA; `uv build` still packages only
`src/statementledger`.

---

## 3. Mental model and storage

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

### 3.1 Storage tree

```
<data>/                                  # STMTLEDGER_WEB_DATA (see §2)
├── accounts/
│   ├── bluelantern-savings/             # one folder per account (slug)
│   │   ├── account.json                 # account metadata + registry key pin
│   │   └── statements/
│   │       ├── 3f9c1a2b4d5e6f70/        # statement id = sha256(pdf)[:16]
│   │       │   ├── original.pdf         # raw upload, byte-for-byte, immutable
│   │       │   ├── statement.json       # canonical dict from sl.parse() (+_key)
│   │       │   └── meta.json            # upload/parse metadata, attempts, history
│   │       └── 9d2e08a1c3b4d5e6/
│   │           ├── original.pdf
│   │           ├── statement.json       # absent until first successful parse
│   │           └── meta.json
│   └── copperkettle-checking/
│       ├── account.json
│       └── statements/…
├── runs/                                # distill-agent run tracking (§8–§9)
│   └── 20261009-141023-a1b2/
│       ├── run.json                     # argv, account, statement ids, pid, status
│       └── events.jsonl                 # trajectory events (§9.1), copied from the run
├── registry/                            # USER REGISTRY overlay: parsers distilled
│   ├── au__bank__product__1.py          # at runtime are USER DATA — saved here by
│   │                                    # the agent (CLI distill runs land in the
│   │                                    # same tree by default when the env var is
│   │                                    # set), shadowing same-key built-ins; never
│   │                                    # committed, never in package source
│   └── .history/                        # revision history (§3.3) — every overlay
│       └── au__bank__product__1/        # save: an immutable blob + one record
│           ├── objects/<sha16>.py       # the saved source, content-addressed
│           └── history.jsonl            # append-only: at, sha16, source, note,
│                                        # verified_against, run_id, parent_builtin_sha
└── cache/
    └── pages/<sha-prefix>/page_0.png    # lazily rasterized PDF pages (§9.3)
```

The agent's own working directory stays where it has always been:
`logs/distill_<YYYYMMDD_HHMMSS>_<stem>/` (see `tools/distill_agent.py`),
referenced from `run.json`. `logs/` is gitignored; so is a repo-relative
`data/`.

### 3.2 `account.json`

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

### 3.3 statement `meta.json`

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
     "parser_sha": "9b1deb4d3b7d4bec",   // sha16 of the exact revision that verified
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
  key, the `parser_sha` of the exact revision that verified, and
  `statementledger.__version__` — a stored ledger stays re-derivable against
  the revision that produced it. A registry *version bump* is a new key
  (`us__bank__product__2`), so upgrading an account = re-pin + re-parse all
  (§7.4); same-key module edits are covered by the same "re-parse all"
  action — overlay edits carry their own revision history
  (`registry/.history/`, §3.1; `tools/registry_history.py` inspects or
  reverts), built-in edits remain a git-pull concern.
- **Period** is derived from transaction dates (min/max of non-null dates;
  rows may legitimately carry `date: null` — `validate()` allows it). Used
  for overlap/chain checks (§10).

---

## 4. The library API the UI sits on

All of this exists today; the web layer is a thin, honest wrapper around it.

### 4.1 Parsing with / without a key

```python
def parse(pdf: str, key: str | None = None) -> dict
```

(`statementledger.parse`.)

- `key` given → only that parser is tried.
- `key=None` → every registered key is tried; **the first that verifies
  wins** (candidates come from `keys()`, sorted).
- Success returns the canonical statement dict tagged with `stmt["_key"]`.
- Failure raises `VerificationError` whose message is
  `no registered parser verified <pdf.name>:` followed by one line per
  candidate attempt, `"<key>: <error>"`. Attempt errors are one of:
  `"TypeName: msg | HINT: …"` (crash + remedial hint), `"schema: …"`
  (validate), `"reconcile: …"` (arithmetic divergence).

Note for the backend: don't string-parse that exception. For anything the
UI shows per-parser, use the structured path below (§4.4).

### 4.2 The canonical statement dict

Guaranteed by `validate()` against `CANONICAL_FIELDS`
(`statementledger.engine`):

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
  "_key": "us__bluelantern__savings__1"         // added by sl.parse
}
```

Extra keys are allowed by `validate()` (it checks presence/types of the
canonical fields, not absence of others) — this is what makes the
provenance convention of §9.5 possible without touching the engine.

### 4.3 Verification surfaces

- `sl.parse()` only ever returns *reconciled* output — verification is the
  gate, not a post-check.
- `validate(stmt) -> list[str]` — schema errors, empty = valid.
- `reconcile_statement(stmt, tol=TOL) -> str | None` — None when it
  reconciles, else the first divergence with row/drift detail. Cheap (pure
  arithmetic); powers a "verify again" badge on statement detail without
  re-parsing.
- `sl.VerificationError`.
- `sl.hint_for(message) -> str` — already embedded in crash attempt errors.

### 4.4 Registry enumeration & structured verdicts (the probe building blocks)

- `sl.keys() -> list[str]` — all registered keys, sorted (built-ins ∪ user
  overlay; globs live, every call). The overlay resolves
  `STATEMENTLEDGER_USER_REGISTRY` → `~/.local/share/statementledger/registry`
  (XDG-aware); the web layer always passes its own `registry/` explicitly.
- `sl.load_module(key, overlay=None)` — imports the overlay copy of
  `<key>.py` when one exists, else the built-in, and checks the `KEY` +
  `parse()` contract. **Execs the file every call — no import caching**,
  so a parser saved by the agent becomes usable by a running web server
  with no restart. Same for `keys()`.
- Revision history (`statementledger.registry`, overlay only — git is
  the built-in changelog): `save_parser(key, code, source, …)` writes the
  immutable content-addressed blob, swaps the live copy atomically, appends
  one `history.jsonl` record (§3.1) and returns the sha16;
  `parser_history(key)`, `revision_code(key, sha16)` and
  `revert_parser(key, sha16)` read back / restore a revision as the new
  head; `parser_file(key)` / `current_sha(key)` say what a load would exec
  and its sha (the provenance behind `parser_sha`, §3.3).
- `sl.frames_for(pdfs, workdir) -> dict` — PDF → frames
  (`statementledger.frames`); returns
  `{"statements": [{"name", "lines": [{"page", "y", "items":
  [[text, x1, x2], …]}, …]}, …]}`. `sl.parse` already wraps this in a
  tempdir — the web layer never manages frames itself.
- `sl.evaluate(parse_fn, statements, key=None) -> dict` — the verdict
  (`statementledger.engine`):

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

### 4.5 CLI (for copy-paste instructions)

`statementledger <pdf> [--key KEY]` (`statementledger.cli`). Distill
agent: `tools/distill_agent.py <pdfs…> [--key K] [--turns N] [--no-save]
[--built-in]`, env
`STATEMENTLEDGER_LLM_URL/MODEL/API_KEY/PROVIDER/REASONING/TURNS/TIMEOUT`
(`.env.example`); a verified run saves into the user overlay with a
revision record (`--built-in`: into `src/statementledger/registry/`, git
its changelog). Registry history: `tools/registry_history.py
list|show|diff|revert`.

---

## 5. HTTP API

Browser GETs on page routes get the SPA shell (§2); the responses below are
what the SPA's JSON fetches receive.

| Method | Path | Request | Success response | Errors |
|---|---|---|---|---|
| GET | `/` | — | accounts list | — |
| POST | `/accounts` | form: `display_name`, `registry_key?` | 303 → `/accounts/{slug}` | 400 empty/invalid name; 409 slug exists |
| GET | `/accounts/{slug}` | — | account detail (statements, chain status, upload zone, active runs) | 404 |
| DELETE | `/accounts/{slug}` | — (confirm dialog) | 204 | 404 |
| POST | `/accounts/{slug}/statements` | multipart: `file` (PDF) | 201 + statement outcome (parsed, failed, or duplicate-notice per §7); stores PDF and meta first, parse outcome in body | 400 not a PDF / empty; 413 > size limit; 404 account |
| POST | `/accounts/{slug}/statements/{id}/parse` | — (uses account pin; body may override `key`) | 200 + `{status: "parsed", summary, period}` or `{status: "failed", attempts: [{key, error}]}` | 404 |
| GET | `/accounts/{slug}/statements/{id}` | — | statement detail (§6.5) | 404 |
| GET | `/accounts/{slug}/statements/{id}/ledger.json` | — | canonical statement dict (§4.2) | 404; 409 not parsed yet |
| GET | `/accounts/{slug}/statements/{id}/ledger.csv` | — | CSV of transactions | 404; 409 |
| GET | `/accounts/{slug}/ledger.json` | — | merged ledger across parsed statements (v2) | 404 |
| DELETE | `/accounts/{slug}/statements/{id}` | — | 204 (removes the statement folder) | 404 |
| POST | `/accounts/{slug}/key` | form: `registry_key` (empty = unpin) | 303 → account page | 400 key not in `sl.keys()`; 404 |
| POST | `/accounts/{slug}/probe` | multipart `file` **or** JSON `{statement_id}` | 200 `{results: [{key, ok, rows, error}], verified_count, suggested_key}` — nothing persisted | 400/404/413 as upload |
| GET | `/registry` | — | parser table (key, description, statements using it) | — |
| GET | `/registry.json` | — | `[{key, description}]` from `sl.keys()` + module docstring first line via `sl.load_module` | — |
| POST | `/runs` | JSON `{account_slug, statement_ids: […], mode: "draft"\|"maintain", key?, rounds?, max_probes?}` | 202 `{run_id}`; spawns the agent (§8.2) | 400 bad payload/key; 404; 409 a run is already active |
| GET | `/runs` | — | active + recent runs with status | — |
| GET | `/runs/{id}` | — | `run.json` state (status, round count, key on success, workdir path) | 404 |
| GET | `/runs/{id}/events` | `?after=<seq>` (reconnect) | `text/event-stream`: SSE of trajectory events (§9.1) | 404 |
| DELETE | `/runs/{id}` | — | 202 (cancelling; terminal event `cancelled`) | 404 |
| GET | `/runs/{id}/rounds/{n}` | — | `{round, kind: "probe"\|"script", code, verdict?}` — `round_N.py` text + gate verdict for the code pane (§9.4) | 404 |
| GET | `/accounts/{slug}/statements/{id}/pages/{n}.png` | — | `image/png` (lazily rasterized, cached under `<data>/cache/pages/`) | 404 |

`GET /runs/{id}/events` keeps SSE semantics: one event per trajectory line,
`id:` = sequence number so reconnects with `?after=` resume without loss.

---

## 6. Pages and wireframes

### 6.1 Accounts list — `/`

```
┌────────────────────────────────────────────────────────────────────────┐
│ statementledger                            [registry: 10 parsers] [+ New account] │
├────────────────────────────────────────────────────────────────────────┤
│ ACCOUNTS (2)                                                           │
│                                                                        │
│ ┌────────────────────────────────────────────────────────────────────┐ │
│ │ Blue Lantern Savings                    ◇ us__bluelantern__savings_1 │ │
│ │ 12 statements · Jan 2026 → Dec 2026 · chain ✓ (11/11 links)        │ │
│ │ latest closing 11,663.82 · updated 2026-10-02               [Open] │ │
│ └────────────────────────────────────────────────────────────────────┘ │
│ ┌────────────────────────────────────────────────────────────────────┐ │
│ │ Copper Kettle Checking                  ◇ us__copperkettle__checking_1│ │
│ │ 8 statements · … · ⚠ chain break: Jun→Jul opening mismatch        │ │
│ │ latest closing 14,972.32 · 1 statement needs attention    [Open]  │ │
│ └────────────────────────────────────────────────────────────────────┘ │
│                                                                        │
│ ┌ + New account ────────────────────────────────────────────────────┐ │
│ │ Display name [___________________]  Parser (optional) [▾ none ▾]  │ │
│ │                                            [Cancel] [Create]      │ │
│ └────────────────────────────────────────────────────────────────────┘ │
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

### 6.3 Upload outcome states (into the statements table / toast)

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
│   $ uv run python tools/distill_agent.py   \                                   │
│         <data>/accounts/bluelantern-savings/statements/3f9c…/original.pdf \    │
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
│ printed 24,868.94 (amount −371.62)   ·   model: <from env> · started 14:10:23        │
└───────────────────────────────────────────────────────────────────────────────────────┘
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
│ │ (row hover: highlight the source line on the rendered page —                │ │
│ │  same overlay component as the run view, §9.5)                              │ │
│ └─────────────────────────────────────────────────────────────────────────────┘ │
│ PARSE HISTORY                                                                     │
│ 2026-10-02 09:15 parsed with us__bluelantern__savings__1 (47 txns)               │
│ 2026-10-02 09:14 failed — 2 attempts (details collapsed)                         │
└───────────────────────────────────────────────────────────────────────────────────┘
```

---

## 7. Upload and parse flow

### 7.1 Happy path (pinned account)

1. `POST /accounts/{slug}/statements` receives the multipart file.
2. Hygiene gate (§10): magic bytes, size, non-empty.
3. `sha256` → id; if the statement folder exists → 409 duplicate outcome.
4. Store `original.pdf` + `meta.json` (`status: "unparsed"`). **The upload
   is now durable; parse failures can never lose the file.**
5. Parse synchronously with the pin: `sl.parse(str(pdf), key=account_key)`
   (§4.1). Success → write `statement.json`, set `status: "parsed"`,
   derive period/summary, append history. Response: outcome 6.3(a).
6. Failure (the pinned parser stopped fitting, e.g. a layout change):
   record attempts (structured, via the probe path §7.3 unrolled for the
   single key), `status: "failed"`, response offers re-detection,
   maintain-mode run, or CLI.

### 7.2 Unpinned account (first upload) — detection

1. Steps 1–4 as above.
2. Extract frames **once** (`sl.frames_for([pdf], tmpdir)`), then for each
   key in `sl.keys()` load the module (`sl.load_module`) and run
   `sl.evaluate(module.parse, statements)` — the structured unrolling of
   `sl.parse`'s loop.
3. Exactly one verifies → parse with that key, save, and **suggest pinning**
   the account to it (one click; `registry_key_source: "auto-detected"`).
4. More than one verifies → surface the candidates and let the user pick
   which to pin (never auto-pin on ambiguity; `sl.parse`'s first-wins rule
   is fine for a one-off parse, not for a permanent account association).
5. None verifies → `status: "failed"`, `attempts` persisted in meta;
   UI state 6.3(c): per-parser failure table + "Register new bank…" +
   copy-paste CLI + "Re-probe parsers" (new registry modules appear in
   `sl.keys()` live — no restart, §4.4).

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
`BackgroundTasks` is the minimal escalation; deliberately not built now.

Upload and parse are **separate endpoints** so that (a) a stored PDF can
be re-parsed later (same code path), and (b) parse failure never loses the
upload — but the upload handler *calls* the parse logic inline so the
first response already has the result.

### 8.2 Distill-agent runs: the one real background job

The agent is minutes-long (default 30 LLM rounds), costs money, needs
`STATEMENTLEDGER_LLM_*` secrets, and runs sandboxed children — it is a
tracked, cancellable subprocess, never inline in a request.

- **Spawn**: `uv run python tools/distill_agent.py <stored original.pdf
  paths…> [--key K] [--rounds N] [--max-probes N] --jsonl-trajectory
  <data>/runs/<id>/events.jsonl` — cwd = repo root, env inherited *plus*
  `.env` loaded via `tools/envloader.py` (the key never reaches the
  browser; the server process itself doesn't need the env at boot).
- **Trajectory as data**: the agent's `Trace` writes `events.jsonl` beside
  its `trace.log` at the existing call sites (`Trace.emit` in
  `tools/distill_agent.py`), always on. The run manager tails it verbatim
  and re-emits it over SSE; `run.json` records argv, pid, account,
  statement ids, status, timestamps — reload-safe.
- **Attach mode / legacy fallback**: a run started manually in a terminal
  (or a pre-emission workdir with only a `trace.log`) is derived, not
  replayed — `web/agent_events.py:TraceDeriver` reconstructs the same
  event vocabulary from the artifacts (`round_N.py`, `gate_round_N.out`,
  `GATE:{json}` lines, the `# probe` first-line convention, `trace.log`
  sections). Attach is tail-watch only: no live selection events (§9.2).
- **Lifecycle**: one active run at a time (409 otherwise — bound the LLM
  spend); `DELETE /runs/{id}` sends SIGTERM to the process group, then
  SIGKILL after a grace period; terminal status (`converged` with saved
  key / `failed` / `cancelled`) is persisted in `run.json` and re-read on
  server restart; a run whose pid is gone on restart is marked `orphaned`
  with its artifacts still browsable. On `converged`, the backend
  auto-parses the pending statement(s) that triggered the run with the new
  key and offers to pin the account (`registry_key_source:
  "auto-detected"`); registry pickup needs no restart (§4.4).

---

## 9. Run view: trajectory + PDF overlay

### 9.1 Trajectory event schema (NDJSON, one object per line)

Emitted by the agent (`Trace.emit`), relayed verbatim as SSE by the
backend (`id:` = line number):

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

Every event type corresponds to a real code site (round loop, `lint_code`,
`py_gate`, `script_feedback`, the parser save); unknown types are ignored
by the UI — the schema is additive.

### 9.2 Live selection events (the overlay's data)

Probes run in a sandboxed child; their only channel out is stdout, which
the parent already captures as data (`run_script`). Convention: a probe
script *may* end by printing one line

```
SELECT:{"page": 0, "lines": [8, 35], "label": "transaction rows"}
```

The parent recognizes the `SELECT:` prefix, resolves `lines` against
`frames.json` into concrete `line_ids` (and a bounding `rect` in PDF
points), and emits the `probe_select` event. The system prompt's probe
instructions teach the convention in one sentence; the `pl.show()` probes
the model already writes can additionally be *approximated* by the parent:
`show(start=S, n=N)` output maps to `line_ids = [S … S+N)` on the
statement's pages — a zero-model-cooperation heuristic that covers most
probes even without the `SELECT:` line. Sandbox integrity is preserved:
selections come out as data on stdout, never as side effects.

**Attach mode** (§8.2): no live selections; the overlay appears only
post-run, on the converged parser, via provenance (§9.5).

### 9.3 Rendering the PDF (and the overlay) — server-side raster

Server-side rasterization with `pypdfium2`: pages rendered lazily to PNG
(scale ~2.0 ≈ 144 dpi), cached content-addressed under
`<data>/cache/pages/<sha-prefix>/page_N.png`; the browser just shows
`<img>`/`<canvas>` tags with absolutely-positioned highlight `<div>`s on
top.

- Why not pdf.js in-browser: it is a large vendored JS blob, and overlay
  alignment must match pdf.js's own text-layer transforms — fiddly and
  JS-debug-heavy. pypdfium2 is a single self-contained wheel (bundled
  pdfium binary), zero system deps, and keeps the coordinate math in
  Python where the frames already live. (PyMuPDF rejected: AGPL.)
- **Coordinate mapping** — frames lines carry `{page, y, items:
  [[text, x1, x2], …]}` in pdfplumber points, y measured **downward from
  the top of the page**; pdfium raster origin is also top-left at natural
  scale = points. So for render scale `s`: `px = x * s`, `py = y * s`, and
  a line highlight band is `rect = [min(x1)*s, (y − H)*s, max(x2)*s,
  (y + 2)*s]` with a default band height `H ≈ 11 pt` (frames store only y
  — the binned bottom edge — not glyph height; one knob, tuned once).
  Whole-line ids are the stable unit: `line_ids` index into the run's
  `frames.json`, which the backend serves next to the page PNGs. Caveat,
  one line: if a PDF has a non-zero cropbox origin or `/Rotate`, add the
  cropbox offset / rotate the overlay (verify once per PDF; the synthetic
  fixtures have neither).
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
localhost; reconnect via `Last-Event-ID`/`?after=`. The SPA folds the
stream into a reducer (`src/pages/run/store.ts`); the PDF overlay,
timeline, and code/trace panes render from state — no imperative DOM
patching, so auto-follow/scrub/code-fetch can't race each other.

### 9.5 Stretch: provenance overlay on any parsed statement

The same PDF+overlay component, post-run and on any statement detail
page: highlight which printed lines produced the opening/closing anchors
and each transaction row. Mechanism: `validate()` ignores extra keys
(§4.2), so parsers may adopt an *optional* provenance convention — e.g.
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
  `prev.closing == next.opening` within `TOL = 0.01`; account header shows
  chain ✓/break with the offending link.
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
  financial data — do not port-forward, do not run on a shared host. If
  exposure is ever needed, a single bearer token + `--host` opt-in is the
  v-later path.
- **Git hygiene**: all PDFs are gitignored except the synthetic fixtures;
  a repo-relative `data/` store and `logs/` are ignored too. Parsed JSON
  contains real transaction data — it must never be committable by
  accident.
- **Concurrency**: one user, but two tabs can race. A per-account
  `threading.Lock` around parse-and-write; statement writes are
  idempotent (content-addressed). Single uvicorn worker.
- **Cost guard on runs**: launch dialog shows model name (from env) and
  round budget; one active run; cancel always one click away.

---

## Appendix: design history

The UI shipped twice. Keeping the record because the *reasons* are still
load-bearing.

**v1 — FastAPI + Jinja2 + htmx (2026-10-09).** The original stack
recommendation optimized for "one runtime dep, Python-only, no npm":
server-rendered HTML fragments swapped in by htmx (vendored), Starlette-
alone and Flask variants weighed and passed on, an SPA explicitly
rejected. Phased plan (1: upload/parse/browse; 2: detection UX, re-parse,
exports; 3: distill runs + trajectory + overlay) — all three shipped,
driven by an external fresh-eyes review loop over a fixed screenshot
tour: 5 rounds, GA: NO → YES. That loop paid for itself before the
reviewer saw a frame: driving the upload through a real browser exposed
that htmx matches `responseHandling` codes as **regexes** — the `"2xx"`
notation never matches status 201, so no successful upload ever swapped
its outcome card in a real browser while every curl-level test was green.
(Operational lesson, kept: curl-green ≠ browser-green.)

**v2 — React SPA (2026-10-09 → 10).** The run view (live SSE trajectory,
scrub/auto-follow, code fetches) pushed past what fragment-swapping could
keep consistent; the SPA (Vite + React + TS + Tailwind, TanStack Query,
react-router) was built at `/app` beside the Jinja UI, talking to the
same content-negotiated routes. Its review loop ran 4 rounds to GA: YES.
On 2026-10-10 the Jinja UI was retired: the SPA became the only UI at `/`
(history fallback, JSON 404s for API clients, legacy `/app/*` 308s), and
the server settled into its current shape — a pure JSON API that
content-negotiates by Accept header. The no-npm ethos survived where it
mattered: the wheel still ships only `statementledger`, and the SPA build
is the only node step in the project.

**Storage** moved from a repo-relative `data/` default to
`STMTLEDGER_WEB_DATA` (durable, repo-independent; `tools/serve_web.sh`
defaults it to `~/.local/share/statementledger`), and runtime-distilled
parsers became user data in that tree's registry overlay with a
content-addressed revision history — the shipped
`src/statementledger/registry/` stays curated source.
