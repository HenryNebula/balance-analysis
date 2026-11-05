"""The parser registry: reuse with zero LLM.

Each registry module lives at statementledger/registry/<key>.py and defines

    KEY = "us__bank__product__1"
    def parse(frames) -> dict: ...

Modules are loaded by key, checked for the contract, and shipped in the
wheel — a registered parser parses FUTURE months of the same bank with no
LLM in the loop. A new bank means one new module (see statementledger.agent).

Two layers, in precedence order:

1. The USER REGISTRY — an overlay directory: STATEMENTLEDGER_USER_REGISTRY
   when set (the web app pins it to its data tree), else
   ~/.local/share/statementledger/registry (XDG_DATA_HOME-aware — the same
   tree serve_web.sh uses for app data). Parsers distilled at runtime are
   USER DATA: they live in the overlay, never inside the repo, and shadow
   same-key built-ins so a maintain run can evolve a shipped parser without
   touching package source. CLI and web treat them identically — a
   hand-run harness saves to the overlay too, and every overlay save goes
   through save_parser(), which keeps a revision history (below).
2. The BUILT-IN registry (this package's registry/ dir) — the curated set
   that ships in the wheel; git is its changelog (distill --built-in).

Revision history (overlay only) — the store.py idiom of content-addressed
blobs plus an append-only log:

    <overlay>/<key>.py                           live copy load_module execs
    <overlay>/.history/<key>/objects/<sha16>.py  immutable revision blobs
    <overlay>/.history/<key>/history.jsonl       one JSON record per save

keys() globs both layers live on every call, so a parser saved by the
distill harness becomes usable by a running web server with no restart.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

REGISTRY = Path(__file__).parent / "registry"
KEY_RE = re.compile(r"^[a-z]{2}__[a-z0-9_]+__[a-z0-9_]+__\d+$")
# Filler words from the harness's template key ("us__bank__product__1") —
# a real bank/product segment is never exactly one of these.
PLACEHOLDER_SEGMENTS = {"bank", "product", "demo"}


def key_problem(key: str) -> str | None:
    """Why `key` cannot be a registry key (None when it can). Beyond the
    KEY_RE shape: exactly four '__'-separated segments (the regex product
    segment would otherwise swallow 'bank__product'), and no unreplaced
    template placeholders left in the bank/product positions — the distill
    drafter has shipped keys like us__bank__liberty_national__credit__1."""
    if not KEY_RE.match(key):
        return f"bad key {key!r} (want cc__bank__product__n, lowercase)"
    segments = key.split("__")
    if len(segments) != 4:
        return (f"bad key {key!r}: {len(segments)} '__'-separated segments, "
                "want exactly 4 (cc__bank__product__n)")
    _cc, bank, product, _n = segments
    if bank in PLACEHOLDER_SEGMENTS or product in PLACEHOLDER_SEGMENTS:
        return (f"bad key {key!r}: the bank/product segments {bank!r}/{product!r} "
                "look like unreplaced template placeholders — derive them from "
                "the statement (e.g. sg__standard_chartered__savings__1)")
    return None


def user_registry(overlay: Path | None = None) -> Path:
    """The overlay directory — user-distilled parsers, shadowing the built-in
    registry. Resolution order: `overlay` argument (the web layer pins each
    app's own data tree — the env var is process-global and wrong when
    several app instances coexist, e.g. in tests), then
    STATEMENTLEDGER_USER_REGISTRY, then the XDG default — so a distill run
    started from a shell also treats its parser as user data, never as
    package source."""
    if overlay is not None:
        return overlay
    path = os.environ.get("STATEMENTLEDGER_USER_REGISTRY", "").strip()
    if path:
        return Path(path)
    base = os.environ.get("XDG_DATA_HOME", "").strip() or "~/.local/share"
    return Path(base).expanduser() / "statementledger" / "registry"


def keys(overlay: Path | None = None) -> list[str]:
    """All registered parser keys, sorted (built-in + user overlay)."""
    found = {path.stem for path in REGISTRY.glob("*.py")}
    found |= {path.stem for path in user_registry(overlay).glob("*.py")}
    return sorted(found)


def parser_file(key: str, overlay: Path | None = None) -> Path | None:
    """The file load_module() would exec — the overlay copy when one exists,
    else the built-in, else None. The overlay-shadows-built-in fallback that
    the harness baseline read and the web run view also need."""
    problem = key_problem(key)
    if problem:
        raise ValueError(problem)
    candidate = user_registry(overlay) / f"{key}.py"
    if candidate.exists():
        return candidate
    builtin = REGISTRY / f"{key}.py"
    return builtin if builtin.exists() else None


def module_path(key: str, overlay: Path | None = None) -> Path:
    """Where a parser with this key is SAVED — always the overlay:
    runtime-distilled parsers are user data (save_parser() creates the
    tree on first write). The built-in registry is written only by an
    explicit `distill --built-in` run, never as a default."""
    return user_registry(overlay) / f"{key}.py"


def load_module(key: str, overlay: Path | None = None):
    """Import the parser module for a key (overlay shadows built-in) and
    check it declares KEY + parse()."""
    import importlib.util

    path = parser_file(key, overlay)
    if path is None:
        raise FileNotFoundError(
            f"no registered parser {key!r} (looked in "
            f"{user_registry(overlay)} and {REGISTRY})")
    spec = importlib.util.spec_from_file_location(
        f"statementledger_registry_{key}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "KEY", None) != key or not callable(getattr(module, "parse", None)):
        raise ValueError(f"{path} must define KEY = {key!r} and def parse(frames)")
    return module


# --- Revision history (overlay only) ---------------------------------------
#
# Content-addressed blobs + an append-only JSONL log, the web/store.py
# idiom: each save writes objects/<sha16>.py once (immutable), swaps the
# live <key>.py atomically (tmp + os.replace), and appends one record.
# Write order is crash-safe — a record never references a missing object,
# and a live copy newer than the history head is detectable via
# current_sha(). Built-ins keep git as their changelog instead.

_SAVE_LOCK = threading.Lock()  # threads in this process; separate processes
# (web app vs. hand-run harness) rely on os.replace atomicity — one-line
# appends interleave at worst, never corrupt.


def _sha16(data: bytes) -> str:
    return sha256(data).hexdigest()[:16]


def _history_dir(key: str, overlay: Path | None = None) -> Path:
    return user_registry(overlay) / ".history" / key


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def current_sha(key: str, overlay: Path | None = None) -> str | None:
    """sha16 of whichever parser file load_module() would exec — the overlay
    copy or the built-in (None when the key is registered nowhere). Pin
    this into run/parse records for provenance: any stored ledger stays
    re-derivable against the exact revision that produced it."""
    path = parser_file(key, overlay)
    return _sha16(path.read_bytes()) if path else None


def parser_history(key: str, overlay: Path | None = None) -> list[dict]:
    """The key's save records, oldest first ([] when none). Blank or
    corrupt lines are skipped, not fatal — same torn-tail tolerance as the
    run event log."""
    log = _history_dir(key, overlay) / "history.jsonl"
    if not log.exists():
        return []
    records = []
    for line in log.read_text().splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def revision_code(key: str, sha16: str, overlay: Path | None = None) -> str:
    """The stored source of one revision."""
    path = _history_dir(key, overlay) / "objects" / f"{sha16}.py"
    if not path.exists():
        have = ", ".join(r["sha16"] for r in parser_history(key, overlay)) or "none"
        raise FileNotFoundError(
            f"no revision {sha16!r} for {key!r} (have: {have})")
    return path.read_text()


def save_parser(key: str, code: str, overlay: Path | None = None, *,
                source: str, note: str = "",
                verified_against: tuple[str, ...] = (),
                run_id: str | None = None) -> str:
    """Save a parser revision to the overlay and return its sha16: the
    content-addressed blob (written once, immutable), the live <key>.py
    (atomic swap — a running server picks it up on the next load_module),
    and one appended history.jsonl record. Identical content to the current
    head is a no-op: no duplicate record, but the live copy is still
    rewritten, healing a hand-edited drift."""
    problem = key_problem(key)
    if problem:
        raise ValueError(problem)
    hist = _history_dir(key, overlay)
    objects = hist / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    digest = _sha16(code.encode())
    with _SAVE_LOCK:
        history = parser_history(key, overlay)
        live = user_registry(overlay) / f"{key}.py"
        if history and history[-1]["sha16"] == digest:
            _atomic_write(live, code)  # dedupe: no record, just heal drift
            return digest
        # First overlay save of a shipped parser: record what is being
        # shadowed, so the fork point stays diffable forever after.
        parent = None
        if not history and not live.exists():
            builtin = REGISTRY / f"{key}.py"
            if builtin.exists():
                parent = _sha16(builtin.read_bytes())
        blob = objects / f"{digest}.py"
        if not blob.exists():
            _atomic_write(blob, code)
        _atomic_write(live, code)
        record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "sha16": digest, "source": source, "note": note,
                  "verified_against": list(verified_against), "run_id": run_id,
                  "parent_builtin_sha": parent}
        with open(hist / "history.jsonl", "a") as log:
            log.write(json.dumps(record) + "\n")
    return digest


def revert_parser(key: str, sha16: str, overlay: Path | None = None, *,
                  note: str = "") -> str:
    """Restore a stored revision as the new head — the revert itself goes
    through save_parser(source='revert'), so history stays append-only.
    Reverting to the current head is a no-op (dedupe)."""
    code = revision_code(key, sha16, overlay)
    return save_parser(key, code, overlay, source="revert", note=note)
