"""Account/statement storage: the filesystem is the store (no database).

One account = one folder under <data_root>/accounts/<slug>/; one statement =
one content-addressed folder statements/<sha256[:16]>/ holding the immutable
original.pdf, the derived statement.json (written only after a verified
parse), and meta.json (upload metadata, status, append-only parse history).
Layout and field names follow docs/web-ui-design.md §2.

Thread safety: a per-account lock (account_lock) serializes parse-and-write
per account; JSON writes are atomic (tmp file + os.replace) so a reader
never sees a torn file.
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class AccountExists(Exception):
    """A different account already occupies this slug."""


class DuplicateStatement(Exception):
    """This exact PDF (same sha256) is already stored in the account."""

    def __init__(self, statement_id: str):
        super().__init__(statement_id)
        self.statement_id = statement_id


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(display_name: str) -> str:
    """Server-generated slug: lowercase, [^a-z0-9]+ -> '-', trimmed, <=64.
    No user string is ever used as a path component unchecked (§10)."""
    slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")
    return slug[:64].rstrip("-") or "-"


def statement_id(pdf_bytes: bytes) -> str:
    return sha256(pdf_bytes).hexdigest()[:16]


class Store:
    def __init__(self, data_root: Path):
        self.root = Path(data_root)
        self.accounts_root = self.root / "accounts"
        self.accounts_root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- locking ---------------------------------------------------------

    @contextmanager
    def account_lock(self, slug: str):
        with self._locks_guard:
            lock = self._locks.setdefault(slug, threading.Lock())
        with lock:
            yield

    # -- accounts ----------------------------------------------------------

    def _account_dir(self, slug: str) -> Path:
        path = (self.accounts_root / slug).resolve()
        if not path.is_relative_to(self.accounts_root.resolve()):
            raise ValueError(f"bad slug {slug!r}")
        return path

    def accounts(self) -> list[dict]:
        out = []
        for account_file in sorted(self.accounts_root.glob("*/account.json")):
            out.append(json.loads(account_file.read_text()))
        return out

    def get_account(self, slug: str) -> dict | None:
        path = self._account_dir(slug) / "account.json"
        return json.loads(path.read_text()) if path.exists() else None

    def create_account(self, display_name: str, registry_key: str | None,
                       key_source: str | None) -> dict:
        slug = slugify(display_name)
        if not SLUG_RE.match(slug):
            raise ValueError(f"name {display_name!r} yields unusable slug {slug!r}")
        directory = self._account_dir(slug)
        if directory.exists():
            raise AccountExists(slug)
        (directory / "statements").mkdir(parents=True)
        account = {"slug": slug, "display_name": display_name.strip(),
                   "registry_key": registry_key,
                   "registry_key_source": (key_source if registry_key else None),
                   "created_at": now(), "notes": ""}
        self._write_json(directory / "account.json", account)
        return account

    def update_account(self, account: dict) -> None:
        directory = self._account_dir(account["slug"])
        self._write_json(directory / "account.json", account)

    def delete_account(self, slug: str) -> None:
        directory = self._account_dir(slug)
        if not (directory / "account.json").exists():
            raise FileNotFoundError(slug)
        for entry in reversed(list(directory.rglob("*"))):
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
            elif entry.is_dir():
                entry.rmdir()
        directory.rmdir()

    # -- statements ----------------------------------------------------------

    def _statements_root(self, slug: str) -> Path:
        return self._account_dir(slug) / "statements"

    def _statement_dir(self, slug: str, sid: str) -> Path:
        if not isinstance(sid, str) or not re.match(r"^[0-9a-f]{16}$", sid):
            raise ValueError(f"bad statement id {sid!r}")
        return self._statements_root(slug) / sid

    def statement_exists(self, slug: str, sid: str) -> bool:
        return (self._statement_dir(slug, sid) / "meta.json").exists()

    def statements(self, slug: str) -> list[dict]:
        """All statement metas, oldest period first (unparsed/failed last)."""
        metas = [json.loads(meta_file.read_text())
                 for meta_file in self._statements_root(slug).glob("*/meta.json")]
        metas.sort(key=lambda m: (m.get("period") and m["period"].get("start")) or "9999")
        return metas

    def statement_meta(self, slug: str, sid: str) -> dict | None:
        path = self._statement_dir(slug, sid) / "meta.json"
        return json.loads(path.read_text()) if path.exists() else None

    def statement_ledger(self, slug: str, sid: str) -> dict | None:
        path = self._statement_dir(slug, sid) / "statement.json"
        return json.loads(path.read_text()) if path.exists() else None

    def original_pdf(self, slug: str, sid: str) -> Path:
        return self._statement_dir(slug, sid) / "original.pdf"

    def store_upload(self, slug: str, pdf_bytes: bytes, sid: str,
                     sha256_hex: str, original_filename: str) -> dict:
        """Durably store the raw upload + meta (status 'unparsed'). The PDF is
        now safe; parse failures can never lose it (§7.1)."""
        directory = self._statement_dir(slug, sid)
        meta_path = directory / "meta.json"
        if meta_path.exists():
            raise DuplicateStatement(sid)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "original.pdf").write_bytes(pdf_bytes)
        meta = {"id": sid, "sha256": sha256_hex,
                "original_filename": original_filename or f"{sid}.pdf",
                "size_bytes": len(pdf_bytes), "uploaded_at": now(),
                "status": "unparsed", "period": None, "summary": None,
                "history": []}
        self._write_json(meta_path, meta)
        return meta

    def save_parsed(self, slug: str, sid: str, stmt: dict, key: str,
                    statementledger_version: str,
                    parser_sha: str | None = None) -> dict:
        """Overwrite statement.json (derived data) and append one history
        entry; re-parses never erase history (§2.3). `parser_sha` pins the
        exact registry revision that produced this ledger (provenance:
        a stored ledger stays re-derivable against it)."""
        directory = self._statement_dir(slug, sid)
        self._write_json(directory / "statement.json", stmt)
        meta = json.loads((directory / "meta.json").read_text())
        dates = [t["date"] for t in stmt["transactions"] if t.get("date")]
        meta["period"] = ({"start": min(dates), "end": max(dates)}
                          if dates else None)
        meta["summary"] = {"transactions": len(stmt["transactions"]),
                           "opening_balance": stmt["opening_balance"],
                           "closing_balance": stmt["closing_balance"]}
        meta["status"] = "parsed"
        meta["history"].append({"at": now(), "outcome": "parsed", "key": key,
                                "parser_sha": parser_sha,
                                "statementledger": statementledger_version})
        self._write_json(directory / "meta.json", meta)
        return meta

    def save_failed(self, slug: str, sid: str, attempts: list[dict],
                    note: str) -> dict:
        directory = self._statement_dir(slug, sid)
        meta = json.loads((directory / "meta.json").read_text())
        meta["status"] = "failed"
        meta["history"].append({"at": now(), "outcome": "failed",
                                "attempts": attempts, "note": note})
        self._write_json(directory / "meta.json", meta)
        return meta

    def delete_statement(self, slug: str, sid: str) -> None:
        directory = self._statement_dir(slug, sid)
        if not (directory / "meta.json").exists():
            raise FileNotFoundError(sid)
        for entry in reversed(list(directory.rglob("*"))):
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
            elif entry.is_dir():
                entry.rmdir()
        directory.rmdir()

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp, path)
