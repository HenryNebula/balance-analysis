# statementledger

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

Runtime dependency: **one** — [pdfplumber](https://github.com/jsvine/pdfplumber).

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

`sl.parse(pdf)` without a key tries every registered parser and returns the
first that verifies.

## Adding a bank: the distill agent

You do not write parsers by hand (though you can — the registry modules are
~60 lines each). `tools/distill_agent.py` runs a tool-calling agent that
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

Configuration via `STATEMENTLEDGER_LLM_*` (copy `.env.example` to `.env`);
any OpenAI-compatible endpoint works, including a local llama-server.
Sandboxed execution (no network, rlimits) and a transcription lint keep the
model honest: it can look, but not smuggle statement data into code.

## Development

```console
$ uv sync
$ uv run pytest                                     # 47 tests
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
