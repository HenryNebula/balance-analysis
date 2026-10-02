"""Pure LLM statement extraction — no config drafting, no registry reuse.

Runs the fork's local LLM fallback directly on a statement and prints the
transactions it read, always validated against the statement's own
opening/closing balances. Use it for one-off statements (a bank not worth
authoring a config for) or as the review-then-correct step: --csv writes
the numbers for manual checking before they are loaded anywhere.

The rules engine still gets first refusal inside the parser (statements
from banks with a known config parse exactly, in milliseconds); the LLM
takes over only when no config applies — for an unknown bank this is a
pure LLM extraction. Lenient mode returns the best attempt with every
validation error attached rather than raising.

Usage (from balance-analysis/):
    uv run python llm_extract.py statement.pdf
    uv run python llm_extract.py statement.pdf --csv out.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from envloader import load_env
from transtractor import Parser

load_env()


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("pdf", type=Path, help="bank statement PDF")
    cli.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="also write the transactions to this CSV for review/correction",
    )
    args = cli.parse_args()

    parser = Parser()  # env-configured: TRANSTRACTOR_LLM_URL / _MODEL / _TIMEOUT ...
    llm = parser.llm_config
    if llm is None:
        print(
            "No LLM endpoint configured. Set TRANSTRACTOR_LLM_URL and "
            "TRANSTRACTOR_LLM_MODEL (or put them in .env)."
        )
        return 1
    print(f"extracting with {llm.model} at {llm.base_url} ...")
    sd = parser.parse(str(args.pdf), lenient=True)

    print(f"\n{args.pdf.name}: {sd.account_number or '(account number not found)'}")
    print(
        f"opening {sd.opening_balance:,.2f} -> closing {sd.closing_balance:,.2f} "
        f"| {len(sd.transactions)} transactions | {sd.key or 'provenance unknown'}"
    )
    print(f"{'date':<12}{'description':<42}{'amount':>13}{'balance':>14}")
    for transaction in sd.transactions:
        description = transaction.description[:40]
        print(
            f"{transaction.date.isoformat():<12}{description:<42}"
            f"{transaction.amount:>13,.2f}{transaction.balance:>14,.2f}"
        )

    if args.csv:
        sd.to_csv(str(args.csv))
        print(f"\ncsv written: {args.csv}")

    if sd.errors:
        print(
            f"\nNEEDS REVIEW — {len(sd.errors)} validation error(s), the numbers "
            "do NOT reconcile with the printed balances:"
        )
        for error in sd.errors:
            print(f"  - {error}")
        return 2
    print("\nRECONCILED — opening + amounts = closing within 0.01.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
