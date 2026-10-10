# Maintenance mode

parser.py already contains a REGISTERED parser that verifies at least one
statement; the failing ones are in this run's statement set. Rules:

- KEY stays exactly as declared — never rename, never register a new key.
- Make the SMALLEST change that fixes the failing statement(s) WITHOUT
  breaking the passing one(s). run gates parse() on EVERY statement; all
  must verify. Prefer a new fallback branch or a widened condition over a
  restructure; use edit, not a rewrite.
- The baseline verdict below names each failing statement and the exact
  error (crash hint, schema row, or reconcile divergence). Read it before
  touching anything.
- When a statement prints section totals ("Total deposits and other
  additions", ...), bind them into totals[] — independent accuracy checks
  that catch errors end-to-end reconcile mathematically cannot see.
- The ONE restructure maintenance may not avoid: a statement holding
  SEVERAL accounts needs parse() to return a LIST of canonical dicts, one
  per account (each reconciles on its own) — even when the baseline's
  single-account dict already verifies.
