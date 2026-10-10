# Freely available bank-statement datasets for robustness testing

Researched and URL-verified 2026-10-09. Every URL below was resolved with `curl`
at research time (except where noted). Goal: real-world statement **documents**
the per-bank parser registry has never seen — many banks, countries, and layouts
beats volume from one bank. PDFs with a text layer are the primary target;
scanned/image PDFs are a deliberate stress case; CSV transaction dumps are not
the target (listed separately at the bottom).

`hf` = the Hugging Face CLI (`pip install -U huggingface_hub`; older installs use
`huggingface-cli download ...` with identical arguments).

## Ranked shortlist

### 1. AgamiAI/Indian-Bank-Statements (Hugging Face) — best bulk corpus, full ground truth

- URL: https://huggingface.co/datasets/AgamiAI/Indian-Bank-Statements
- Contents: 100 synthetic Indian **business (current-account)** statements, each
  rendered 4 ways: `Digital_Type1/2` (text-layer PDF, ~150 KB) and
  `Scanned_Type1/2` (image-only PDF, ~6.5 MB) — 400 PDFs + 400 matching JSON
  files total (~1.4 GB full; ~45 MB digital-only). Two table layouts: separate
  Debit/Credit columns vs single signed Transaction column.
- License: Apache-2.0. PII: none — fully synthetic (fictional banks such as
  "Progressive National Bank", "Trustmark Bank"; README states all data is
  algorithmically generated).
- Ground truth: yes, excellent — per-statement JSON contains `bank_name`,
  `account_number`, `opening_balance`, `closing_balance`, and every transaction
  with `date, narration, debit/credit, balance, value_date, cheque_no` — almost a
  1:1 match for statementledger's canonical dict. Lets us score extraction
  accuracy, not just reconciliation.
- Verified: sampled JSONs across the corpus (160+ transactions per statement,
  distinct bank names); downloaded one digital PDF (text layer present, and it
  includes a diagonal watermark whose glyphs interleave in the text layer — good
  noise stress) and one scanned PDF (`pdftotext` returns nothing — genuinely
  image-only).
- Caveats: one country, one currency (INR), essentially 2 layout families
  (x2 renderings); business accounts only.

```bash
# digital-only subset (~45 MB): text-layer PDFs + JSON ground truth
hf download AgamiAI/Indian-Bank-Statements --repo-type dataset \
  --local-dir data/agami \
  --include "train/India_Bank_Statement_Digital_Type1/*" \
            "train/India_Bank_Statement_Digital_Type2/*"

# or everything, including 200 scanned image-PDFs (~1.4 GB) for the no-text-layer stress path
hf download AgamiAI/Indian-Bank-Statements --repo-type dataset --local-dir data/agami
```

Why: 400 PDFs runnable today through pdfplumber with exact expected ledgers; the
scanned variants exercise the OCR-degradation path; Apache-2.0 and zero PII mean
no hygiene headaches.

### 2. Bankstatemently Open Benchmark (Hugging Face + GitHub) — purpose-built, multi-country, adversarial

- URLs: https://huggingface.co/datasets/Bankstatemently/bank-statement-parsing-benchmark ;
  https://github.com/bankstatemently/bank-statement-parsing-benchmark (MIT, actively maintained)
- Contents: synthetic statements from fictional banks across 12 countries / 8
  languages in 3 difficulty tiers. **5 of 15 PDFs released so far** (bsb-001..005:
  Singapore, US, NL, HK, Canada; 12-25 transactions each) + `metadata.jsonl` with
  country, currency, language, pages, transactionCount, and per-file `challenges`
  tags.
- License: MIT. PII: none (synthetic fictional banks, e.g. "Straits Capital").
- Ground truth: intentionally NOT shipped — scoring is via their free evaluation
  API (https://bankstatemently.com/developers/api); `metadata.jsonl` at least
  gives expected transaction counts.
- Caveats: tiny today (5 files; the other 10 are "coming soon" per the GitHub
  repo). The changelog warns PDFs are occasionally re-issued — pin sha256s from
  `metadata.jsonl` when comparing runs.
- Verified: fetched `metadata.jsonl` and file tree on HF.

```bash
hf download Bankstatemently/bank-statement-parsing-benchmark --repo-type dataset \
  --local-dir data/bsb
git clone --depth 1 https://github.com/bankstatemently/bank-statement-parsing-benchmark
```

Why: the only public benchmark deliberately designed for statement parsing, and
its challenge tags ("balance-validation", "credit-debit-columns",
"posting-date-selection", "multi-line-descriptions", "two-digit-year-dates",
"currency-symbol-amounts", "multiple-accounts-multiple-tables") read like a test
plan for our reconciliation gate. Best per-file layout diversity available.

### 3. Bank- and government-published sample statements — real layouts, zero PII

Single PDFs published by banks/regulators as customer-facing samples. Fictional
account data, real bank layout engines. All URLs verified HTTP 200:

- Chase (US, credit card, paperless-statement demo):
  https://www.chase.com/content/dam/chase-ux/documents/digital/resources/paperless_statements_chase_sample.pdf
- CFPB (US government, public domain, credit card statement handout):
  https://files.consumerfinance.gov/f/documents/cfpb_building_block_activities_sample-credit-card-statement_handout.pdf
  (uses placeholder dates like `12/30/XX` — a cute date-format edge case)
- Impact Bank (Canada, generic "Statement of Account"):
  https://www.impact-bank.com/user/file/dummy_statement.pdf

```bash
mkdir -p data/samples && cd data/samples
curl -sL -o chase_paperless_sample.pdf   https://www.chase.com/content/dam/chase-ux/documents/digital/resources/paperless_statements_chase_sample.pdf
curl -sL -o cfpb_credit_card_sample.pdf  https://files.consumerfinance.gov/f/documents/cfpb_building_block_activities_sample-credit-card-statement_handout.pdf
curl -sL -o impact_bank_sample.pdf       https://www.impact-bank.com/user/file/dummy_statement.pdf
```

Why: real Chase typography/spacing as produced by a real bank's statement
system; safe to keep and redistribute-with-care. License note: bank-copyrighted
but published for customers (fine for internal testing; don't re-host).
Visa's Practical Money Skills lesson PDFs also embed sample statements
(e.g. `.../pdfs/lessons/lev_3/L3TeachersGuide6.pdf`) but the CDN 403s curl —
browser download only, so not primary.

### 4. GitHub parser projects bundling sample statements — real-world fixtures

Two flavors: (a) **real statements with real PII** — great format coverage, must
stay out of git; (b) fictional/synthetic one-offs — safe.

Real-PII fixtures (do not commit):

- devcenter-square/ng-bank-parser (Nigeria; GPL-2.0):
  PDF fixtures for **Access Bank, Ecobank, FirstBank, Heritage Bank, UBA**
  (`lib/ng-bank-parser/fixtures/*-pdf-valid.pdf`), plus valid/invalid pairs.
  Verified real: the Access Bank fixture contains a real customer name and
  account number.
  `git clone --depth 1 https://github.com/devcenter-square/ng-bank-parser`
- JashBarbhaya/Credit-card-statement-parser: `samples/` holds HDFC and Axis
  (India) credit-card PDFs plus the Chase/CFPB/AmEx samples mirrored above.
  `git clone --depth 1 https://github.com/JashBarbhaya/Credit-card-statement-parser`
- Sne81/ai_agent_challenge: one ICICI (India) statement as PDF **and** its
  expected CSV — a rare matched parse-target/answer pair from a real bank.
  https://raw.githubusercontent.com/Sne81/ai_agent_challenge/main/data/icici/icici_sample.pdf
- nwtaf/bank-statement-extraction: pdfplumber demo with one Chase statement
  (`src/paperless_statements_chase_sample.pdf`).

Safe fictional one-offs:

- aws-samples/textract-bank-statement-processor (MIT-0): `Statement20230428.pdf`
  — verified a 1-page **image-only** PDF with "ACME BANK / John Doe" content:
  perfect tiny scanned-statement stress case, no PII.
  https://raw.githubusercontent.com/aws-samples/textract-bank-statement-processor/main/Statement20230428.pdf
- XiaoNaZha/fictional-bank-statement-csv-sample (CC0 intent stated in README):
  one fictional PDF + matching CSV/XLSX/JSON/schema — matched pair.
  https://raw.githubusercontent.com/XiaoNaZha/fictional-bank-statement-csv-sample/main/sample-bank-statement.pdf
- jahfaliabdulrahman-dev/rajhi-rag: `data/sample/statement_sample.pdf` —
  synthetic Al Rajhi (Saudi Arabia) statement; Arabic/RTL layout diversity we
  have nowhere else.
  https://raw.githubusercontent.com/jahfaliabdulrahman-dev/rajhi-rag/main/data/sample/statement_sample.pdf

All raw URLs above verified 200. Licenses: repo code licenses (GPL-2.0, MIT-0,
none) do not necessarily cover the statement PDFs themselves; embedded real
statements remain the bank's/customer's — testing use only.

### 5. harshitraj2409/bank-statement-vlm (Hugging Face) — page images + full JSON ground truth

- URL: https://huggingface.co/datasets/harshitraj2409/bank-statement-vlm
- Contents: 846 train + 94 validation **page images** (not PDFs) of statements
  from 5 Indian banks — SBI (195 pages), HDFC (189), Bank of Baroda (171),
  Canara (147), Axis (144) — each with a `label` JSON containing header fields
  (opening/closing balance) and every visible transaction. ~69 MB.
- License: **none declared** (treat as all-rights-reserved: local testing only,
  no redistribution). PII: synthetic-looking (generic names), real bank names.
- Verified: sampled rows via the HF datasets-server API.
- `hf download harshitraj2409/bank-statement-vlm --repo-type dataset --local-dir data/bsvlm`
- Why: only multi-bank set with per-page ground truth; useful if we ever wrap
  pages into PDFs or test an image path. Ranked mid-list because format is
  images and licensing is unspecified.

### 6. tusharshah2006/bank_statements_transactions (Hugging Face) — photographed passbooks, real PII

- URL: https://huggingface.co/datasets/tusharshah2006/bank_statements_transactions
- Contents: 120 train + test/val JPGs of **physical passbook pages** (Bank of
  Baroda, HDFC, IOB, Janaseva, UCO — India). Verified by inspecting an image:
  real photographed documents with real account numbers in transaction lines.
- License: none declared. PII: **real** — download locally, never commit.
- `hf download tusharshah2006/bank_statements_transactions --repo-type dataset --local-dir data/passbooks`
- Why: worst-case-input stress (phone photos of passbooks, not statements) for
  any OCR/robustness experiment; low value for the pdfplumber path.

## Adopted local corpora (in data/, gitignored — never committed)

Two corpora are downloaded and in active use. Both carry a `metadata.jsonl`
with sha256 pins next to the files; `uv run python tools/bsb_matrix.py <dir>`
sweeps a corpus against the whole registry.

### data/bsb — Bankstatemently benchmark (see shortlist #2)

5 PDFs, sha256-pinned against upstream `metadata.jsonl`; expected
transaction counts used as ground truth by the matrix. All five banks have
distilled, verified registry parsers (see `docs/web-ui-design.md` context in
git history: `benchmark: distill Bankstatemently parsers`).

### data/templatelab — TemplateLab statement templates, converted to PDF

- Source: https://templatelab.com/bank-statement/ (35 editable "bank
  statement" templates; several mimic real banks: JPMorgan Chase x3 incl.
  the Toronto branch, Guaranty Trust Bank NG, plus generic/blank templates,
  reconciliation exercises, a 30-page investment report, and Excel statement
  generators). Fictional data, no PII.
- The live site is Cloudflare-gated for scripted clients, so the files were
  taken from Wayback Machine captures (2019) of the page's
  `?wpdmdl=<id>` download endpoints. Those captures turned out to be the
  templates' **Word/Excel originals**, not the site's PDFs — they were
  converted to PDF locally with headless LibreOffice. Each file's exact
  provenance (wayback URL + origin format) is recorded per-file in
  `data/templatelab/metadata.jsonl`.
- License posture: templates are free to download but © TemplateLab — fine
  for local parser testing, NOT redistributable; this is why the corpus
  lives only in gitignored `data/` and this section records where it came
  from. No ground-truth transaction counts (`transactionCount: null`), so
  the matrix checks verification/false-positives only.
- Current status: 17 statements, none verified (no parsers for these
  layouts yet — a distill to-do list), zero false positives from the
  existing registry.

## Secondary — adjacent datasets (NOT statement documents)

**Document-image datasets (images, no PDFs, need conversion for our pipeline):**

- Panhapich/bank-statement-detection and Panhapich/bank-statement-structure-recognition
  (https://huggingface.co/datasets/Panhapich/bank-statement-structure-recognition) —
  MIT; 15,187 synthetic ReportLab-rendered statement images with table bounding
  boxes. Clean license, synthetic, layout-only (no transaction GT).
- rikeshVertex/invoice-receipt-cheque-bankstatement-dataset (+ identical copies
  under deepthink8/, cublya/) — 4,262 images labeled invoice/receipt/cheque/
  bank_statement with JSON ground truth; **no license**; origin/synthesis unclear.
- devpatel18042004/receipts-invoices-bank-statements — 150K images (50K bank
  statements) shipped as a single **33 GB RAR**; no license; unwieldy.
- rita1706/dotsocr_bank_statement_1K_v2 — 1,000 pages of **real OCBC (SG/ID)
  statements** as JPGs + OCR/layout JSON; no license; real PII.

**Transaction-record-only CSV/JSON (fraud/analytics-shaped, no documents):**

- Kaggle (pages exist; file listings sit behind login — verify after download;
  anonymous API downloads 404, so use the CLI with a free account):
  - `kaggle datasets download -d abutalhadmaniyar/bank-statements-dataset` (2022-23 transactions)
  - `kaggle datasets download -d abhinav4651/sample-bank-statement-dataset`
  - `kaggle datasets download -d bhavyasingh25/financial-transaction-description-dataset` (5,000 narrations)
  - `kaggle datasets download -d apoorvwatsky/bank-transaction-data`
  - Possibly document-shaped (unverified): `shregar/doc-bank-statement-detect`
    ("clean, digitally-generated bank statement PDFs" per its description) and
    `abishek9324/bank-statement-multiformat-parsing` — worth a first download.
- Hugging Face: karthiksagarn/bank-statement-categorization (CSV),
  radil-454/bank-statement-transactions (JSON), Phu-Hien/bank-statement-structured (JSON).
- bank2ynab/bank2ynab (GitHub) — CSV export fixtures for hundreds of real banks
  worldwide; useful to sanity-check our canonical-dict field coverage against
  what banks actually expose, but CSV-only.

**Synthetic generators:**

- We already generate our own (tools/fixtures/gen_fixtures.py — the only PDFs
  allowed in git). Panhapich's dataset proves ReportLab renders are a viable
  cheap route to more layout families (MIT example to copy from).
- Bankstatemently's generator is not open-sourced; Docsumo's "samples" are a
  commercial trial, not public files. Deliberately excluded: "fake bank
  statement generator" sites (proof-of-income-fraud tools, malware-adjacent).

## Ruled out (checked, dead ends)

- Zenodo: "bank statements" queries return only academic finance *papers*
  (financial-statement analysis), no statement-document corpora.
- Academic doc-understanding benchmarks: FUNSD, CORD, XFUND, DocVQA, DocILE,
  DocBank, FinixDocBench (inclusionAI), BaFCo (arXiv 2607.05614 — Bangla
  government forms) — none contain retail bank statements.
- American Express: no official public sample-statement PDF exists (only
  third-party mirrors inside repo #4 above).
- Practical Money Skills PDFs: exist but CDN 403s non-browser clients.
- Template.net "sample bank statements": account-walled marketing templates,
  not real layouts.
- Numerous "bank statement" Kaggle/HF hits are fraud-detection transaction CSVs
  mislabeled as statements (e.g. the PaySim-alikes) — relegated to secondary.
- Parser repos without bundled PDFs (marlanperumal/pdf_statement_reader,
  jakubzapletal/bank-statements, heypoom/kbank-statement-pdf-parser,
  felgru/bank-statement-parser, karlicoss/hsbc-parser, electrovir/statement-parser)
  — the READMEs target real statements but ship none.
- ManojKumarPatnayak/Major-project-list (surfaces in searches): a project-ideas
  list, irrelevant.

## PII / licensing hygiene for this repo

- The repo already gitignores `*.pdf` (only `tests/fixtures/*.pdf` from
  `tools/fixtures/gen_fixtures.py` are allowed). Keep it that way: everything
  above downloads into a gitignored `data/` dir — add `data/` to `.gitignore`
  so JSONs/images from these sets can't leak in either.
- Real-PII sets (ng-bank-parser fixtures, tusharshah2006 passbooks, dotsocr OCBC
  pages, and any user-supplied statement): local use only, never committed,
  never re-uploaded. Prefer masked/derived snippets if a fixture must be quoted
  in an issue.
- Synthetic sets with clean licenses (AgamiAI Apache-2.0, Bankstatemently MIT,
  Panhapich MIT, aws-samples MIT-0, XiaoNaZha CC0) are safe for local test data
  and safe to derive committed, re-generated fixtures from — but do not
  vendor the downloaded PDFs themselves into git; regenerate or script the
  download (a `tools/fetch_test_data.py` keyed by URL + sha256 would match the
  repo's existing fixture philosophy).
- Unlicensed HF uploads (harshitraj2409, rikeshVertex, devpatel18042004,
  tusharshah2006, rita1706) default to all-rights-reserved: fine to test
  against locally, not okay to redistribute or commit.
- Bank-published samples (Chase/CFPB/Impact) are the banks' copyright: testing
  yes, re-hosting no.
- Pin versions: Bankstatemently re-issues PDFs (their changelog); record the
  sha256s from `metadata.jsonl` in test manifests so results stay comparable.
