# Response contract

- ONE complete script in a single ```python fence: imports, KEY, def
  parse(frames), demo under if __name__ == "__main__".
- KEY is cc__bank__product__n, lowercase, from THE STATEMENT's country,
  printed bank name, and product — never bare "bank"/"product"/"demo".
- One account: the canonical dict. Several accounts in one PDF (repeated
  headers, each with own balances): a LIST of dicts, one per account, all
  covered — a single ledger over a multi-account PDF is partial coverage
  the gate cannot see.
- Under ~70 lines. No try/except — the traceback comes back verbatim.
- Literals only for label terms / header words — never amounts, dates,
  descriptions.
- READ amounts from the amount columns; never compute them from balances
  (a tautology the gate cannot see). Bind printed activity totals into
  totals[].
- Probes (first line "# probe") run any time and are discarded — only a
  full script converges the run (past 90% of turns you are reminded).
- Read the END of the statement before trusting a stop word; grep instead
  of guessing a label.
- Never finish while your own pl.reconcile prints FAIL.
