"""Verification matrix for a downloaded statement corpus.

For every statement in a corpus dir (default data/bsb/data — see
docs/bank-statement-datasets.md; any dir with *.pdf + metadata.jsonl works)
x every registered parser: does it verify? Then flag anything that is not a
clean 1:1 mapping:

  - a statement with no verifier (distill it: statementledger-distill)
  - a statement with >1 verifier (ambiguity: pin the account to one key)
  - a verifier whose transaction count != the corpus metadata's count
    (verified arithmetic over partial coverage — e.g. one account of a
    multi-account PDF; skipped when the metadata's transactionCount is null,
    i.e. the corpus ships no ground truth)

    uv run python tools/bsb_matrix.py [corpus_dir]
"""
import argparse
import json
import tempfile
from pathlib import Path

import statementledger as sl
from statementledger.registry import keys, load_module


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("corpus", nargs="?", type=Path, default=Path("data/bsb/data"),
                     help="dir with *.pdf + metadata.jsonl (default: data/bsb/data)")
    data = cli.parse_args().corpus
    meta_path = data / "metadata.jsonl"
    if not meta_path.exists():
        cli.error(f"no metadata.jsonl in {data} (see docs/bank-statement-datasets.md)")

    meta = {m["id"]: m for m in (json.loads(line) for line
                                 in meta_path.read_text().splitlines() if line.strip())}
    key_names = keys()
    print(f"registered parsers ({len(key_names)}): {key_names}\n")

    report = {}
    for mid, m in sorted(meta.items()):
        pdf = data / m["file_name"]
        with tempfile.TemporaryDirectory() as tmp:
            statements = sl.frames_for([pdf], Path(tmp))["statements"]

        expected = m.get("transactionCount")
        row = {"challenges": m.get("challenges"), "expected_txns": expected,
               "verifiers": []}
        for key in key_names:
            if sl.evaluate(load_module(key).parse, statements)["verified"]:
                ledgers = sl.ledgers_of(load_module(key).parse(statements[0]))
                n_txns = sum(len(ledger["transactions"]) for ledger in ledgers)
                row["verifiers"].append({
                    "key": key, "n_txns": n_txns,
                    "txns_match_meta": None if expected is None else n_txns == expected,
                    "banks": [ledger["bank_name"] for ledger in ledgers],
                    "accounts": [ledger["account_number"] for ledger in ledgers],
                })
        report[mid] = row

    print(json.dumps(report, indent=1))

    flags = []
    for mid, r in report.items():
        if not r["verifiers"]:
            flags.append(f"{mid}: NO verifier")
        elif len(r["verifiers"]) > 1:
            flags.append(f'{mid}: {len(r["verifiers"])} verifiers '
                         f'({", ".join(v["key"] for v in r["verifiers"])})')
        for v in r["verifiers"]:
            if v["txns_match_meta"] is False:
                flags.append(f'{mid}: {v["key"]} verified but '
                             f'{v["n_txns"]}/{r["expected_txns"]} txns (partial coverage?)')
    print("\n" + ("\n".join(flags) if flags else "clean 1:1 mapping, all txn counts match"))
    return 1 if flags else 0


if __name__ == "__main__":
    raise SystemExit(main())
