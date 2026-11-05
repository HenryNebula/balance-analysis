# statementledger

[![CI](https://github.com/HenryNebula/balance-analysis/actions/workflows/ci.yml/badge.svg)](https://github.com/HenryNebula/balance-analysis/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/statementledger.svg)](https://pypi.org/project/statementledger/)

Bank statement PDFs to verified ledgers.

`statementledger` parses a statement PDF and checks the arithmetic before
returning anything: the opening balance plus all signed transaction
amounts must equal the closing balance, printed running balances must
track row by row, and printed section totals must match the rows they
summarize. A parse that fails any check raises `VerificationError` naming
the exact divergence.

## Install

```console
$ pip install statementledger
```

Runtime dependencies: pdfplumber (layout extraction), pikepdf (PDF triage
and font healing), jinja2 (prompt templates for the distill agent).

## Use

```python
import statementledger as sl

stmt = sl.parse("statement.pdf", key="us__bank__product__1")
# {'bank_name': ..., 'account_number': ..., 'opening_balance': ...,
#  'closing_balance': ..., 'transactions': [{'date': 'YYYY-MM-DD',
#   'description': ..., 'amount': ..., 'balance': ...}, ...]}
```

Or from the shell:

```console
$ statementledger statement.pdf --key us__bank__product__1
```

Without a key, `sl.parse(pdf)` tries every registered parser and returns
the first result that verifies. When none verifies, `VerificationError`
carries one line per parser with its specific failure.

A multi-account PDF (several accounts, each with its own balances) returns
a list of dicts, one per account, each verified. `sl.parse_all(pdf)`
always returns a list.

## How it works

Extraction reduces the PDF to **frames**: the printed layout as plain
data, `{"lines": [{"page", "y", "items": [[text, x1, x2], ...]}, ...]}`.

Each supported bank product has a **registry module**, keyed
`cc__bank__product__n` (country, bank, product, version). The module is a
pure `parse()` function from frames to the canonical statement dict, with
every value derived at runtime. A registered parser reads future months
of the same bank with no LLM involvement.

**The gate.** A parser's output must pass `validate()` (schema) and
`reconcile()` (arithmetic). A parser enters the registry only after its
output reconciles on the reference statements, and `sl.parse()` re-runs
the same checks on every call.

## The registry

Ten modules ship in `src/statementledger/registry/`:

- `us__copperkettle__checking__1`: debit/credit columns, CR-suffixed
  balances, multi-page table with repeated furniture, wrapped and
  date-less rows. The stress test.
- `us__bluelantern__savings__1`: one signed Amount column with
  `(parenthesised)` negatives. The minimal example; read it first.
- Eight more distilled from statement templates:
  `ca__harbour_bank__business__1`, `ca__jpmorgan__dda__1`,
  `hk__silk_road__business__1`, `nl__continentaltrust__personal__1`,
  `sg__standard_chartered__savings__1`, `us__fidelity__portfolio__1`,
  `us__jpmorgan_chase__checking__1`, `us__liberty_national__credit__1`.

A key names a real institution where the layout was learned from that
bank's statement template. The module contains layout logic only: all
amounts, dates, and descriptions are computed from the PDF at runtime,
and the distill gate lints parser code against transcribed statement
data.

Parsers distilled at runtime are user data. They live in an overlay
directory: `STATEMENTLEDGER_USER_REGISTRY` when set, otherwise
`~/.local/share/statementledger/registry` (XDG-aware). The overlay
shadows same-key built-ins, and every overlay save goes through
`save_parser()`, which records a revision history:

    <overlay>/<key>.py                           live copy (what loads)
    <overlay>/.history/<key>/objects/<sha16>.py  immutable revision blobs
    <overlay>/.history/<key>/history.jsonl       one record per save

Inspect and revert with `tools/registry_history.py` (`list` / `show` /
`diff` / `revert`; refs are a sha16, `HEAD`, `HEAD~n`, and `LIVE` on
diff). Built-ins carry no history tree; git is their changelog.

## Adding a bank: the distill agent

You do not have to write parsers by hand (the modules run 72 to 137
lines). `statementledger-distill`, the console script for
`statementledger.agent`, runs a tool-calling agent that explores the
statement with render, grep, table, and python tools, builds `parser.py`
step by step with write and edit tools, and gates it with `run` after
every change. A run that verifies registers the parser; nothing is saved
before that.

```console
$ statementledger-distill newbank_statement.pdf
$ statementledger-distill --key us__newbank__checking__1 \
      january.pdf february.pdf   # MAINTAIN: evolve a registered parser
```

In a repo checkout: `uv run statementledger-distill ...` or
`uv run python -m statementledger.agent ...`.

A verified run saves into the user overlay with a revision record;
`--no-save` drafts without writing anything. `--built-in` writes into
`src/statementledger/registry/` instead, to promote a parser into the
curated set; commit the result.

Configure the endpoint with `STATEMENTLEDGER_LLM_*` variables (copy
`.env.example` to `.env`). Any OpenAI-compatible endpoint works,
including a local llama-server. Probes run sandboxed (no network,
rlimits), and a transcription lint rejects parser code that embeds
statement data.

## Web UI (local)

`web/` is a single-user web UI over the library. It lives in the repo and
is not part of the package. Accounts are folders, statements upload by
drag and drop, parsing goes through the registry, and verification
failures appear as outcomes with per-parser attempts rather than stack
traces. The UI is a React SPA (`web/frontend`, Vite + TypeScript) served
by a FastAPI JSON API. There is no database: state lives on the
filesystem under `STMTLEDGER_WEB_DATA` (repo-relative `data/` by default;
`tools/serve_web.sh` sets it to `~/.local/share/statementledger`).
Design: [docs/web-ui-design.md](docs/web-ui-design.md).

```console
$ tools/serve_web.sh
```

That script builds the SPA on first run and binds 127.0.0.1:8765. By
hand:

```console
$ uv sync --group web
$ cd web/frontend && npm ci && npm run build && cd ../..
$ uv run --group web uvicorn web.app:app --host 127.0.0.1 --port 8765
```

There is no authentication and no multi-user support; bind to localhost
only. Statements are sensitive financial data, so do not port-forward or
run the app on a shared host.

When no registered parser verifies a statement, the UI offers a distill
run: launch it (draft for a new bank, or maintain to evolve the pinned
parser), then watch it live with a round timeline, gate verdicts, and a
PDF overlay showing what each probe examined. On convergence the pending
statements are parsed automatically with the new parser. Runs need
`STATEMENTLEDGER_LLM_*` in `.env`. One run at a time, and cancel is one
click. `/runs` lists every run; a run against an existing harness
workdir can be attached in tail-watch mode.

The web dependencies live in a separate dependency group. node and npm
are needed only to build the SPA, and the wheel still ships only the
`statementledger` package.

## Development

```console
$ uv sync --group web
$ uv run --group web pytest                         # ~340 tests
$ uv run python tools/fixtures/gen_fixtures.py      # regenerate fixtures
$ uv build                                          # wheel: package only
```

Test fixtures are synthetic: fictional banks generated with the ledger's
ground truth (`tests/fixtures/*.truth.json`). No real statement PDF is
ever committed; `.gitignore` enforces this, with the fixture generator as
the only exception path.

## Credits

The ledger-verification principle and the frames extraction algorithm
come from [transtractor](https://github.com/weberdak/transtractor-lib)
(MIT, Daniel Weber). The Python port in `statementledger/frames.py` is
verified item for item against the rust engine. See [NOTICE](NOTICE) for
full attributions.

MIT licensed. See [LICENSE](LICENSE).
