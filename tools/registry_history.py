"""Inspect and manage the user registry's revision history (overlay only —
built-ins keep git as their changelog; see statementledger/registry.py).

    uv run python tools/registry_history.py list [key] [--overlay DIR]
    uv run python tools/registry_history.py show <key> <ref>
    uv run python tools/registry_history.py diff <key> <a> <b>
    uv run python tools/registry_history.py revert <key> <ref>

A ref is a revision sha16, HEAD (the newest record), or HEAD~n (n records
back). diff also accepts LIVE — the file a load would exec right now — to
answer "what changed since the last save?". --overlay defaults to the
user_registry() resolution (STATEMENTLEDGER_USER_REGISTRY, else
~/.local/share/statementledger/registry).
"""
import argparse
import difflib
import re
import sys
from pathlib import Path

from statementledger.registry import (parser_file, parser_history,
                                      revert_parser, revision_code,
                                      user_registry)

_HEAD_RE = re.compile(r"^HEAD~(\d+)$")


def _fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _resolve_sha(key: str, ref: str, history: list[dict],
                 overlay: Path) -> str:
    """HEAD → the newest record, HEAD~n → n back, else a literal sha16."""
    if ref == "HEAD":
        if not history:
            _fail(f"{key} has no history yet")
        return history[-1]["sha16"]
    back = _HEAD_RE.match(ref)
    if back:
        n = int(back.group(1))
        if n >= len(history):
            _fail(f"{ref} out of range — {key} has {len(history)} revision(s)")
        return history[-1 - n]["sha16"]
    if ref not in {record["sha16"] for record in history}:
        have = ", ".join(record["sha16"] for record in history) or "none"
        _fail(f"no revision {ref!r} for {key!r} (have: {have})")
    return ref


def _content(key: str, ref: str, history: list[dict], overlay: Path) -> str:
    if ref == "LIVE":
        path = parser_file(key, overlay)
        if path is None:
            _fail(f"{key} is not registered (no live file)")
        return path.read_text()
    return revision_code(key, _resolve_sha(key, ref, history, overlay), overlay)


def _cmd_list(args: argparse.Namespace) -> int:
    overlay = user_registry(args.overlay)
    if not args.key:
        hist_root = overlay / ".history"
        tracked = (sorted(p.name for p in hist_root.iterdir() if p.is_dir())
                   if hist_root.exists() else [])
        if not tracked:
            print(f"no revision history under {overlay}")
            return 0
        for key in tracked:
            history = parser_history(key, args.overlay)
            head = history[-1] if history else None
            print(f"{key}  {len(history)} revision(s)  "
                  f"head {head['sha16'] if head else '-'} "
                  f"at {head['at'] if head else '-'}")
        return 0
    history = parser_history(args.key, args.overlay)
    if not history:
        print(f"{args.key}: no history (built-ins have none — "
              "git is their changelog)")
        return 0
    for record in reversed(history):  # newest first
        run = f" run {record['run_id']}" if record.get("run_id") else ""
        note = f"  {record['note']}" if record.get("note") else ""
        print(f"{record['at']}  {record['sha16']}  "
              f"{record['source']}{run}{note}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    history = parser_history(args.key, args.overlay)
    print(_content(args.key, args.ref, history,
                   user_registry(args.overlay)), end="")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    history = parser_history(args.key, args.overlay)
    overlay = user_registry(args.overlay)
    a = _content(args.key, args.a, history, overlay)
    b = _content(args.key, args.b, history, overlay)
    diff = difflib.unified_diff(a.splitlines(), b.splitlines(),
                                fromfile=f"{args.key}@{args.a}",
                                tofile=f"{args.key}@{args.b}", lineterm="")
    print("\n".join(diff))
    return 0


def _cmd_revert(args: argparse.Namespace) -> int:
    history = parser_history(args.key, args.overlay)
    if args.ref == "LIVE":
        _fail("LIVE is not a revert target (it is the current file)")
    sha16 = _resolve_sha(args.key, args.ref, history,
                         user_registry(args.overlay))
    head = revert_parser(args.key, sha16, args.overlay,
                         note=f"revert to {sha16} via tools/registry_history.py")
    print(f"{args.key} reverted to {sha16} — head is now {head}; "
          "keys()/load_module pick the live copy up with no restart")
    return 0


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        parser = sub.add_parser(name, help=help_text)
        parser.add_argument("--overlay", type=Path, default=None,
                            help="overlay dir (default: "
                                 "STATEMENTLEDGER_USER_REGISTRY else "
                                 "~/.local/share/statementledger/registry)")
        return parser

    listing = add("list", "keys with history, or one key's records "
                          "(newest first)")
    listing.add_argument("key", nargs="?", default=None)

    show = add("show", "print a revision's source")
    show.add_argument("key")
    show.add_argument("ref", help="sha16 | HEAD | HEAD~n | LIVE")

    differ = add("diff", "unified diff between two refs")
    differ.add_argument("key")
    differ.add_argument("a", help="sha16 | HEAD | HEAD~n | LIVE")
    differ.add_argument("b", help="sha16 | HEAD | HEAD~n | LIVE")

    revert = add("revert", "restore a revision as the new head")
    revert.add_argument("key")
    revert.add_argument("ref", help="sha16 | HEAD | HEAD~n")

    args = cli.parse_args()
    return {"list": _cmd_list, "show": _cmd_show,
            "diff": _cmd_diff, "revert": _cmd_revert}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
