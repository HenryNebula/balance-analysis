"""Benchmark driver: run the distill agent over a corpus N times each.

    uv run python tools/bench_qwen.py data/bsb/data --runs 5 [--turns 120]

Sequential (one local model slot). Each run is `python -m
statementledger.agent <pdf> --no-save --turns N` with PYTHONPATH pinned
to THIS repo's src/ — the
editable install can be re-pointed at a worktree by any concurrent `uv
sync`, and a benchmark must not silently change engines mid-sweep. A
preflight import check fails the row loudly instead of letting every run
die at startup. Resumable: rows that already logged a workdir are skipped,
so a crashed sweep restarts where it stopped. Results append as JSONL
(default logs/bench_results.jsonl); a running summary lands beside it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
from statementledger.env import load_env  # noqa: E402

load_env()

AGENT = ["-m", "statementledger.agent"]
ENV = {**os.environ, "PYTHONPATH": str(REPO / "src")}


def preflight() -> None:
    """Fail fast when the venv's statementledger is not this repo's."""
    probe = subprocess.run(
        [sys.executable, "-c", "import statementledger; "
         "print(statementledger.__file__)"],
        capture_output=True, text=True, env=ENV, cwd=REPO)
    path = probe.stdout.strip()
    if probe.returncode != 0 or not path.startswith(str(REPO / "src")):
        raise SystemExit(
            f"preflight failed: statementledger resolves to {path or probe.stderr!r}"
            f" — expected {REPO / 'src'} (a worktree uv sync stole the venv; "
            "fix with: uv sync --reinstall-package statementledger)")


def parse_stats(workdir: Path | None, stdout: str) -> dict:
    stats = {"verified": "VERIFIED" in stdout and "FAILED" not in stdout}
    if workdir is None or not workdir.exists():
        stats["error"] = "no workdir"
        return stats
    log = (workdir / "trace.log").read_text()
    turns = [int(n) for n in re.findall(r"^turn (\d+):", log, re.M)]
    chars = [int(n) for n in re.findall(r"(\d+)k chars", log)]
    stats.update({
        "turns": max(turns) if turns else 0,
        "peak_kchars": max(chars) if chars else 0,
        "writes": log.count("write parser.py"),
        "edits": log.count("edit parser.py"),
        "gates": len(re.findall(r"turn \d+: \d+/\d+ clean", log)),
        "elide_scheduled": log.count("context elided"),
        "elide_forced": log.count("forced elision"),
        "duplicates": log.count("duplicate "),
        "refusals": log.count("REFUSED"),
        "loop_aborts": log.count("loop abort"),
        "advisories": log.count("ADVISORY"),
        "chat_fails": log.count("chat failed"),
    })
    parser = workdir / "parser.py"
    if parser.exists():
        key = re.search(r'^KEY = "(.*?)"', parser.read_text(), re.M)
        if key:
            stats["key"] = key.group(1)
    ledgers = re.findall(r"  (\S+\.pdf) \[([^\]]*)\]: (\d+) transactions",
                         stdout)
    if ledgers:
        stats["ledgers"] = [{"account": account, "txns": int(txns)}
                            for _name, account, txns in ledgers]
    if not stats["verified"]:
        gate_errors = re.findall(r'"error": "([^"]{0,120})', log)
        stats["last_error"] = gate_errors[-1] if gate_errors else \
            (stdout.strip().splitlines() or ["?"])[-1][:120]
    return stats


def rows(path: Path) -> list[dict]:
    if path.exists():
        return [json.loads(line) for line in path.read_text().splitlines()
                if line.strip()]
    return []


def summary(path: Path, docs: list[str]) -> str:
    known = rows(path)
    lines = []
    for doc in docs:
        mine = [r for r in known if Path(doc).name == r["doc"]]
        if not mine:
            continue
        ok = sum(1 for r in mine if r["verified"])
        times = sorted(r["elapsed_s"] for r in mine)
        turns = sorted(r.get("turns", 0) for r in mine)
        lines.append(f"{Path(doc).name}: {ok}/{len(mine)} verified · turns "
                     f"{'/'.join(str(t) for t in turns)} · "
                     f"median {times[len(times) // 2] // 60}m")
    return "\n".join(lines)


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("corpus", type=Path, help="directory of statement PDFs")
    cli.add_argument("--runs", type=int, default=5, help="runs per document")
    cli.add_argument("--turns", type=int, default=120,
                     help="agent turns per run")
    cli.add_argument("--results", type=Path,
                     default=REPO / "logs" / "bench_results.jsonl")
    cli.add_argument("--status", type=Path,
                     default=REPO / "logs" / "bench_status.txt")
    args = cli.parse_args()

    docs = sorted(str(p) for p in args.corpus.glob("*.pdf"))
    if not docs:
        raise SystemExit(f"no PDFs in {args.corpus}")
    preflight()

    already = {(r["doc"], r["run"]) for r in rows(args.results)
               if r.get("workdir")}
    if already:
        print(f"resuming: {len(already)} attempt(s) already logged",
              flush=True)
    else:
        args.results.write_text("")

    model = os.environ.get("STATEMENTLEDGER_LLM_MODEL", "?")
    endpoint = os.environ.get("STATEMENTLEDGER_LLM_URL", "?")
    total = len(docs) * args.runs
    done = len(already)
    for doc in docs:
        for i in range(1, args.runs + 1):
            if (Path(doc).name, i) in already:
                continue
            started = time.time()
            proc = subprocess.run(
                [sys.executable, *AGENT, doc, "--no-save",
                 "--turns", str(args.turns)],
                capture_output=True, text=True, env=ENV, cwd=REPO)
            elapsed = round(time.time() - started)
            match = re.search(r"workdir: (\S+)", proc.stdout)
            workdir = REPO / match.group(1) if match else None
            row = {"doc": Path(doc).name, "run": i, "rc": proc.returncode,
                   "elapsed_s": elapsed, "model": model, "endpoint": endpoint,
                   "workdir": str(workdir.relative_to(REPO))
                   if workdir else None}
            row.update(parse_stats(workdir, proc.stdout))
            with args.results.open("a") as handle:
                handle.write(json.dumps(row) + "\n")
            done += 1
            args.status.write_text(
                f"[{done}/{total}] {Path(doc).name} run {i}: "
                f"rc={proc.returncode} verified={row['verified']} "
                f"turns={row.get('turns')} {elapsed // 60}m{elapsed % 60:02d}s"
                f" ({time.strftime('%H:%M')})\nmodel {model} at {endpoint}\n\n"
                + summary(args.results, docs) + "\n")
            print(f"[{done}/{total}] {Path(doc).name} #{i} "
                  f"verified={row['verified']} turns={row.get('turns')} "
                  f"{elapsed}s", flush=True)
    print("\nFINAL\n" + summary(args.results, docs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
