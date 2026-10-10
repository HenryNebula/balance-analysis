# Response contract

- ONE complete script in a single ```python fence: imports, KEY,
  def parse(frames), then a demo guarded by if __name__ == "__main__".
- KEY is cc__bank__product__n, lowercase, from THE STATEMENT's country,
  printed bank name, and product — never bare "bank"/"product"/"demo".
- Under ~70 lines.
- Probes (first line "# probe") run any time and are discarded — only a
  full script converges the run (past 90% of turns you are reminded).
- Read the END of the statement before trusting a stop word; grep instead
  of guessing a label.
- Never finish while your own pl.reconcile prints FAIL.
