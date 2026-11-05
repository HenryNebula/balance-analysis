# statementledger

[![CI](https://github.com/HenryNebula/balance-analysis/actions/workflows/ci.yml/badge.svg)](https://github.com/HenryNebula/balance-analysis/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/statementledger.svg)](https://pypi.org/project/statementledger/)

Bank statement PDFs to **verified** ledgers.

```python
import statementledger as sl

stmt = sl.parse("statement.pdf", key="us__bank__product__1")
# {'bank_name': ..., 'account_number': ..., 'opening_balance': ...,
#  'closing_balance': ..., 'transactions': [{'date': 'YYYY-MM-DD',
#   'description': ..., 'amount': ..., 'balance': ...}, ...]}
# A multi-account PDF (several accounts, each with its own balances) returns
# a LIST of these, one per account — sl.parse_all(pdf) normalizes to a list.
```

Or from the shell:

```console
$ statementledger statement.pdf --key us__bank__product__1
```

Runtime dependencies: **two** — [pdfplumber](https://github.com/jsvine/pdfplumber)
(layout extraction) and pikepdf (PDF triage / font healing).

## The idea

A bank statement is a ledger, and a ledger is *verifiable*: opening +
signed amounts must equal closing, printed running balances must track,
printed section totals must match their parsed sums. Everything else in
statement extraction is trust-me output — plausible-looking rows nobody
checked. Here, arithmetic is the gate:

- A statement PDF is reduced to **frames** — the printed layout as plain
  data: `{"lines": [{"page", "y", "items": [[text, x1, x2], ...]}, ...]}`.
- Each supported bank has a tiny **registry module** — a pure `parse()`
  function that turns frames into the canonical statement dict, deriving
  every value at runtime (no transcribed amounts, dates, or descriptions).
  A registered parser therefore reads *future months* of the same bank with
  zero LLM.
- A parser only ships if its output passes `validate()` + `reconcile()`.
  `sl.parse()` re-runs the gate on every call: if no registered parser
  reconciles a statement, you get a `VerificationError` naming each
  failure — never silent garbage.

## The registry

Parsers live in `src/statementledger/registry/`, one module per bank
product, keyed `cc__bank__product__n` (country, bank, product, version):

- `us__copperkettle__checking__1` — debit/credit columns, CR-suffixed
  balances, multi-page table with repeated furniture, wrapped and date-less
  rows (the stress test)
- `us__bluelantern__savings__1` — single signed Amount column with
  `(parenthesised)` negatives (the minimal example — read it first)
- eight more distilled from statement templates: `ca__harbour_bank__business__1`,
  `ca__jpmorgan__dda__1`, `hk__silk_road__business__1`,
  `nl__continentaltrust__personal__1`, `sg__standard_chartered__savings__1`,
  `us__fidelity__portfolio__1`, `us__jpmorgan_chase__checking__1`,
  `us__liberty_national__credit__1`

A key names a real institution only where the parser was distilled from
that bank's statement template — the module is layout logic and nothing
else: every value is derived at runtime from the PDF, and the distill gate
lints parser code against transcribed statement data.

`sl.parse(pdf)` without a key tries every registered parser and returns the
first that verifies.

Two layers, in precedence order: the **user overlay** —
`STATEMENTLEDGER_USER_REGISTRY` when set, else
`~/.local/share/statementledger/registry` (XDG-aware; with the default
`STMTLEDGER_WEB_DATA`, that is also where the web app's overlay lives) —
and the built-ins above. The overlay
shadows same-key built-ins, and every overlay save goes through
`save_parser()`, which keeps a revision history:

    <overlay>/<key>.py                           live copy (what loads)
    <overlay>/.history/<key>/objects/<sha16>.py  immutable revision blobs
    <overlay>/.history/<key>/history.jsonl       one record per save

Inspect and revert with `tools/registry_history.py` (`list` / `show` /
`diff` / `revert`; refs are a sha16, `HEAD`, `HEAD~n` — and `LIVE` on
diff, to see what changed since the last save). Built-ins carry no history
tree; git is their changelog.

## Adding a bank: the distill agent

You do not write parsers by hand (though you can — the registry modules
run 72–137 lines). `tools/distill_agent.py` runs a tool-calling agent that
explores the statement (render/grep/table/python tools), builds `parser.py`
step by step (write/edit tools), and gates it on the same arithmetic
(`run`) — a VERIFIED run registers the parser; nothing ships until it
reconciles:

```console
$ uv run python tools/distill_agent.py newbank_statement.pdf
...
$ uv run python tools/distill_agent.py --key us__newbank__checking__1 \
      january.pdf february.pdf   # MAINTAIN: evolve a registered parser
```

A verified run saves into the user overlay with a revision record
(`--no-save` drafts without writing anything). To promote a parser into the
curated shipped set, `--built-in` writes `src/statementledger/registry/`
instead — commit the result; git is the built-in changelog.

Configuration via `STATEMENTLEDGER_LLM_*` (copy `.env.example` to `.env`);
any OpenAI-compatible endpoint works, including a local llama-server.
Sandboxed execution (no network, rlimits) and a transcription lint keep the
model honest: it can look, but not smuggle statement data into code.

## Web UI (local)

`web/` is a single-user web UI over the library: accounts as folders,
drag-in PDF upload, parse with the registry, and hard verification failures
shown as first-class outcomes (per-parser attempts, not stack traces). The
UI is a React SPA (`web/frontend`: Vite + TypeScript) served by a FastAPI
JSON API that content-negotiates the same URLs for browsers and the app.
No database — the filesystem under `STMTLEDGER_WEB_DATA` (repo-relative
`data/` by default; `tools/serve_web.sh` sets it to
`~/.local/share/statementledger`) is the store. Design:
[docs/web-ui-design.md](docs/web-ui-design.md).

The one-liner — builds the SPA on first run, binds 127.0.0.1:8765:

```console
$ tools/serve_web.sh
```

or by hand (note the SPA build step — without it the server is API-only):

```console
$ uv sync --group web
$ cd web/frontend && npm ci && npm run build && cd ../..
$ uv run --group web uvicorn web.app:app --host 127.0.0.1 --port 8765
```

The web dependencies live in a separate dependency group; node/npm is only
needed to build the SPA; the wheel still ships only `statementledger`.
**No auth, localhost only** — statements are sensitive financial data. Do
not port-forward or run on a shared host.

Storage is durable and repo-independent: everything the app writes at
runtime — accounts, statements, runs, **and parsers distilled through the
UI** — lives under `STMTLEDGER_WEB_DATA`. Distilled parsers go to a
user-registry *overlay* (`<data>/registry/`, shadowing same-key built-ins)
and are user data, never committed — CLI-initiated distills land in the
same tree by default, each save revisioned under
`<data>/registry/.history/`; the shipped `src/statementledger/registry/`
stays curated.

When no registered parser verifies a statement, the UI offers one click into
the distill harness: launch a run (draft for a new bank, maintain to evolve
the pinned parser), watch it live — round timeline, gate verdicts, and a PDF
overlay showing what each probe looked at — and on convergence the pending
statements are parsed automatically with the new parser. Runs need
`STATEMENTLEDGER_LLM_*` in `.env`; one active run at a time; cancel is always
one click. `/runs` lists every run; a run against an existing harness
workdir can be attached (tail-watch, no live selections).

## Development

```console
$ uv sync --group web
$ uv run --group web pytest                         # ~340 tests
$ uv run python tools/fixtures/gen_fixtures.py      # regenerate fixtures
$ uv build                                          # wheel: package + registry only
```

Test fixtures are **synthetic** — fictional banks generated with the
ledger's ground truth (`tests/fixtures/*.truth.json`). Real statements are
real financial data: no real PDF is ever committed (enforced in
`.gitignore`; the generator is the only exception path).

## Credits

The ledger-verification principle and the frames-extraction algorithm are
borrowed from [transtractor](https://crates.io/crates/transtractor) (MIT);
the Python port in `statementledger/frames.py` is verified item-for-item
against the rust engine. See [NOTICE](NOTICE) for full attributions.

MIT licensed — see [LICENSE](LICENSE).
