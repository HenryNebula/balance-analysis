# Maintenance mode

You are revising an EXISTING registered parser that already works for at
least one statement. Rules of maintenance:
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
  the failing statement's header row, its stop words, its money formats.
- The ONE restructure maintenance may not avoid: a statement holding SEVERAL
  accounts needs parse() to return a LIST of canonical dicts, one per
  account (each reconciles on its own) — even when the baseline's
  single-account dict already verifies. Cover every account table.
