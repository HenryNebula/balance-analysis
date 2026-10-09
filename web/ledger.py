"""Ledger-shaped derivations over parsed statements: chain checks, period
overlap/gap warnings, merged account ledgers, CSV export, re-parse drift
reports (docs/web-ui-design.md §7.4, §10 — Phase 2).

All functions are pure: metas (from store) and ledgers (the stored canonical
statement dicts) in, plain data out.
"""

from __future__ import annotations

import csv
import io
from datetime import date


def _parsed_with_period(metas: list[dict]) -> list[dict]:
    return [m for m in metas if m["status"] == "parsed" and m.get("period")]


def chain(metas: list[dict], tol: float) -> dict:
    """Adjacent parsed statements (period order) must satisfy
    prev.closing == next.opening within tol — a header status, never a block
    (§10). Returns {total, ok, breaks: [{from_id, to_id, prev_closing,
    next_opening}]}."""
    parsed = _parsed_with_period(metas)
    links = []
    for prev, nxt in zip(parsed, parsed[1:]):
        prev_closing = prev["summary"]["closing_balance"]
        next_opening = nxt["summary"]["opening_balance"]
        if abs(prev_closing - next_opening) > tol:
            links.append({"from_id": prev["id"], "to_id": nxt["id"],
                          "prev_closing": prev_closing,
                          "next_opening": next_opening})
    return {"total": len(parsed) - 1, "ok": len(parsed) - 1 - len(links),
            "breaks": links}


def period_warnings(metas: list[dict]) -> list[dict]:
    """Overlap or gap between adjacent statements' periods — a warning chip
    per offending statement, never a block (banks reissue corrected
    periods, §10)."""
    warnings = []
    for prev, nxt in zip(_parsed_with_period(metas),
                         _parsed_with_period(metas)[1:]):
        prev_end = date.fromisoformat(prev["period"]["end"])
        next_start = date.fromisoformat(nxt["period"]["start"])
        if next_start <= prev_end:
            warnings.append({"id": nxt["id"], "kind": "overlap",
                             "days": (prev_end - next_start).days + 1})
        elif (next_start - prev_end).days > 1:
            warnings.append({"id": nxt["id"], "kind": "gap",
                             "days": (next_start - prev_end).days - 1})
    return warnings


def merged(account: dict, metas: list[dict],
           ledgers: dict[str, dict]) -> dict:
    """One ledger across every parsed statement: transactions in (date,
    statement) order, each tagged with its statement id."""
    parsed = _parsed_with_period(metas)
    transactions = []
    for order, meta in enumerate(parsed):
        for txn in ledgers[meta["id"]]["transactions"]:
            row = dict(txn)
            row["statement_id"] = meta["id"]
            row["_order"] = order
            transactions.append(row)
    transactions.sort(key=lambda t: (t.get("date") or "9999-99-99",
                                     t["_order"]))
    for row in transactions:
        del row["_order"]
    return {
        "account": {"slug": account["slug"],
                    "display_name": account["display_name"],
                    "registry_key": account["registry_key"]},
        "statements": [{"id": m["id"], "period": m["period"]}
                       for m in parsed],
        "opening_balance": (ledgers[parsed[0]["id"]]["opening_balance"]
                            if parsed else None),
        "closing_balance": (ledgers[parsed[-1]["id"]]["closing_balance"]
                            if parsed else None),
        "transactions": transactions,
    }


def to_csv(ledger: dict) -> str:
    """Transactions as CSV: date,description,amount,balance."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["date", "description", "amount", "balance"])
    for txn in ledger["transactions"]:
        writer.writerow([
            txn.get("date") or "",
            txn.get("description", ""),
            f"{txn.get('amount', 0):.2f}",
            "" if txn.get("balance") is None
            else f"{txn['balance']:.2f}"])
    return buffer.getvalue()


def drift(old_meta: dict | None, new_meta: dict) -> dict:
    """Old→new comparison for one re-parsed statement: status, period, and
    summary. `changed` is flagged loudly in the report UI — the gate should
    prevent silent divergence, but two verified parses can still differ in
    what they read (§7.4)."""

    def snap(meta: dict | None) -> dict:
        return {"status": meta.get("status") if meta else None,
                "period": meta.get("period") if meta else None,
                "summary": meta.get("summary") if meta else None}

    return {"id": new_meta["id"], "old": snap(old_meta), "new": snap(new_meta),
            "changed": snap(old_meta) != snap(new_meta)}
