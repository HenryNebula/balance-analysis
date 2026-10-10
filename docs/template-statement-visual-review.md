# Template-statement visual review log

ui-review-loop for the template-driven statement generator
(`tools/templates/gen_statements.py`). Reviewer: `agy` CLI
(gemini-3.8-flash-medium), fresh conversation per round. Tour:
`tools/templates/render_tour.py` — full pages of the shortest + longest
month, T&C (final) page of every other month; 100 DPI PNGs, re-captured
identically each round.

Context given to the reviewer: print bank statements filled from a
real-world Word template (© TemplateLab, fictional bank "Meridian Trust
Bank"); the goal is that generated months look like one bank's statement
system produced them — layout consistency across months matters more
than beauty, and fidelity to the template outranks cosmetic preference.

## Keep-list (template-authentic, do not re-flag)

- `Ref:` line wraps after the prefix ("Ref: MER 0000-88421-" / "003107")
  — the original template wraps identically at the same width.
- Reward points ": 0.000" three-decimal formatting — template's own.
- T&C section is single-column monospace, no bank letterhead — matches
  the template family's typography; real banks vary here.
- Company name wraps to two lines at 22pt in the left column — the
  original's placeholder wraps at the same point.
- Statement can go negative (Aug overdraft) — deliberate scenario.

## Round 0 — smoke (single 20-txn month, 3 pages)

Findings (agy, gemini-3.8-flash-medium) → outcome:

1. Footer "Page X of ?" broken placeholder — **fix**: template footer is
   a 42.65pt VML textbox with literal " of 2"; rebuilt as
   `Page {PAGE} of {NUMPAGES}` fields and widened the box to 90pt.
2. Address lines "Kingsport…/United States" floated into the top of the
   right column — **fix**: `set_para_text` was assigning text to the run
   carrying the template's column break, deleting it and rebalancing the
   2-column header; text swaps now never touch runs holding
   breaks/drawings. (Root cause of the "misplaced address block".)
3. Orphan tail page (~90% blank, 4 lines) — **fix**: removed 5 of the 6
   empty filler paragraphs after the tail company line.
4. Table header repeat unverifiable (table ended on page 1) — deferred
   to round 1 tour (long months).
5. `Ref:` + `**** END OF STATEMENT ****` wrap — **keep** (template
   wraps identically; see keep-list).
6. WITHDRAW total printed negative ("-60,497.75") — **fix**: totals now
   unsigned sums (`abs()`).
7. Reward points ": 0.000" — **keep** (template's own).
8. Company name duplicated as CUSTOMER NAME — **fix**: contact line
   "Attn: R. Ackroyd" instead.
9. T&C page styling — **keep** (see keep-list).

Verdict: FAILED → fixes applied, tour promoted to the fixed 18-page set.

## Round 1 — full tour (18 pages)

Findings (agy) → outcome:

1. Continuation text colliding with repeated header's dashed border
   (blocker) — **disproven at ink level + restructured**: 300-DPI pixel
   scan shows the border ~9px above the first ink row (the pdfplumber
   "overlap" was the 8pt glyph ascender box, not ink); regardless,
   continuation lines were merged into the transaction row's PARTICULARS
   cell as extra paragraphs (the template's own wrapped-row style), so a
   short continuation row can never land first after a repeated header.
2. T&C clauses differ per month (blocker) — **fix**: one bank, one
   T&C — fixed clause set, fixed order, every month.
3. Orphan page holding only the totals row (minor) — **fix**: the
   template's nextPage section break before the reward-points tail is
   now continuous; the tail follows the table directly (all months
   ≤4 pp).

Verdict: GA: NO.

## Round 2 — tour after fixes (17 pages; shortest 2027-08 + longest
2027-12 in full, T&C pages of the rest)

All explicit checks PASS (header repeat, column alignment, footers,
identical T&C, totals placement). Findings → outcome:

1. Reward tail still on its own page (blocker) — **fix (for real this
   time)**: round 1's converter looked for `w:type val="nextPage"`, but
   the tail's section break has NO `w:type` child (absent type defaults
   to nextPage); the converter now treats a missing type as nextPage
   too. All months 2-4 pages; tail follows the table.
2. `**** END OF STATEMENT ****` wraps ("…STATEMENT / ****") (minor) —
   **keep (template-authentic)**: the original template's own PDF wraps
   the identical line the same way.
3. Double space in "Reward  Points Redeemed/Expired" (nit) — **keep
   (template-authentic)**: the template's own label text.

Verdict: GA: NO (blocker 1 remained at submission time).

## Round 3 — tour after the section-break fix (16 pages; full 2027-08
[2 pp] + 2027-11 [4 pp], T&C pages of the rest)

Findings → outcome:

1. Reward tail split across the page break (labels vs ': values'),
   orphaned values on a near-empty page (blocker) — **fix**: the root
   cause is the template's 2-column section + column-break positioning
   hack, whose layout varies with where it lands. The tail is now
   flattened to atomic `Label : value` lines in a 1-column section
   (keepNext-chained through END OF STATEMENT).
2. Overflow page lacks header/metadata (minor) — subsumed by fix 1
   (the page no longer exists).
3. Reward block layout differs across months (1-col vs 2-col) (minor)
   — **fix**: same flattening; one layout everywhere.

Also added a fit-feedback loop in the generator: after conversion it
checks (pdfplumber) that the tail lands on the table's last page and
nudges the month's transaction count (+1, ≤9 tries, seeded streams
keep the ledger stable) until it does — the way a real statement
engine budgets the last page. Structural scan: 12 statements, 0
failures; tail fits on all 12.

Verdict (round 3 submission): GA: NO.

## Round 4 — flattened atomic tail (17 pages; full 2027-01 [3 pp] +
2027-12 [4 pp], T&C pages of the rest)

Findings → outcome:

1. Tail line mangled: "…Earned In Current Month Reward  Points
   Redeemed/Expired : 61.300" — two labels, one value (blocker) —
   **fix**: the template's tail labels are NOT one per paragraph (one
   paragraph holds two labels separated by a tab), so re-texting in
   place can't work; the block is now rebuilt as four clean
   `Label : value` paragraphs cloned from a donor label.
2. Stray repeated customer header under the totals (minor) — **fix**:
   blanked the tail's letterhead repeat (the head-of-statement
   letterhead keeps the identity; deliberate deviation from the
   template, which repeated it above a standalone tail page).

Verdict: GA: NO.

## Round 5 — same tour re-submitted

All checks PASS: tail placement/format, header repeat, column
alignment, footers, T&C consistency, no collisions or clipping.

Verdict: **GA: YES** — loop stopped after 5 rounds.

## Standing gates (beyond the loop)

- `tools/templates/layout_scan.py` — objective scan (repeated headers,
  cross-page/month column x-invariance, footer correctness, ink-level
  rule-collision check with the 8pt ascender tolerance).
- `tests/test_template_statements.py` — layout scan, truth-ledger
  reconcile + month chaining, length variance, T&C uniformity,
  registry false-positive guard, byte-determinism (regeneration sha256
  == manifest pin; requires soffice), and the UI-style golden pixel
  test (re-render == tour baseline; requires pdftoppm). Tests skip
  where the local corpus (data/, © TemplateLab derivatives) is absent.

## Round 6 — presentation variations (18 pages; full 2027-01 + 2027-04)

Added realism variations, each stressing a different parser assumption:
1-3-line narrations (new 3-line pool), CHQ.NO populated on cheque/branch
rows, inactive money column blank instead of "0.00" on some rows, ~10%
of rows printing no balance (truth keeps the computed running balance).
Manifest now records per-month variation counts; a new no-clipping
tripwire test asserts every narration word survives into the PDF text
layer. Findings → outcome:

1. 3-line narration rows clipped at the bottom (blocker) — **fix, two
   parts**: (a) the template pins row heights per line count (268 for
   1-line, 450 for its 2-line rows) and extrapolating the 450 rounding
   (182/line) under-sized 3-liners by ~2pt — line 3's glyphs crossed the
   next row's top; heights now use the true 12pt line pitch (240tw)
   beyond the template's own 1/2-line values. (b) The template reserves
   no footer band: content runs to y=700pt while the footer rule sits
   absolutely at y=696.55 — bottom rows printed through the rule; bottom
   margin widened to 100pt so content clears it. Transaction rows also
   get cantSplit (a printed row never breaks across pages).
2. Sparse-cell alignment — pass.
3. "Tail orphan / Page 4 of 5" (nit) — **disproved**: the scan verifies
   footers ("Page N of M") and tail-on-table-page for every statement;
   the reviewer's page numbering didn't match the documents.

Note: the round-6 clipping was invisible to text-layer checks (clipped
glyphs still extract) — the agy review caught it, and the objective
scan's ink-level rule test then confirmed it. Eyes and geometry each
cover the other's blind spots.

Verdict: GA: NO.

## Round 7 — same tour after height/footer fixes

All checks PASS: 2-, 3-, and 4-line narrations fully rendered, footer
band clear on every page, sparse cells aligned.

Verdict: **GA: YES** — second convergence (7 rounds total across the
corpus's two configurations).

Keep-list additions from rounds 2-4: END OF STATEMENT two-line wrap and
the double space in the old reward label are the template's own;
LibreOffice renders with pinned fonts (Courier New → Liberation Mono
metric substitute), which is deterministic on a fixed machine profile.
