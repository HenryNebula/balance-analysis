"""CLI: statementledger <pdf> [--key KEY] -> canonical statement JSON."""

from __future__ import annotations

import argparse
import json
import sys

from statementledger import ScannedPdfError, parse


def main() -> int:
    cli = argparse.ArgumentParser(
        description="Parse a bank statement PDF into a verified ledger.")
    cli.add_argument("pdf")
    cli.add_argument("--key", default=None,
                     help="registered parser key (default: try every "
                          "registered parser; the first that verifies wins)")
    args = cli.parse_args()
    try:
        stmt = parse(args.pdf, key=args.key)
    except ScannedPdfError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(stmt, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
