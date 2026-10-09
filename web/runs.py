"""Distill-harness run tracking (docs/web-ui-design.md §8.2, §9): the one
real background job.

The manager spawns tools/distill_harness.py as a tracked subprocess with
--jsonl-trajectory pointing at data/runs/<id>/events.jsonl, tails that file
plus the process exit status (a watcher thread per run), and the app relays
the events over SSE. run.json is the durable record — reload-safe. One
active run at a time (bounded LLM spend). DELETE = SIGTERM to the process
group, SIGKILL after a grace period; the harness emits a terminal
`cancelled` event on SIGTERM. A run whose pid is gone on restart is marked
`orphaned` with its artifacts still browsable. On convergence the pending
statements are auto-parsed with the new key (the registry needs no restart).

Attach mode (§8.2 fallback b, zero harness control): instead of spawning,
the manager watches an existing logs/distill_* workdir and DERIVES events
from its artifacts (round_N.py appearing, GATE: lines in gate_round_N.out,
the trace's result section). No live probe selections — those only exist
with --jsonl-trajectory.
"""

from __future__ import annotations

import json
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import statementledger as sl

from web import parsing
from web.store import Store

ACTIVE_STATES = ("running", "cancelling")
TERMINAL_STATES = ("converged", "failed", "cancelled", "orphaned")
KILL_GRACE_SECONDS = 5.0
WATCH_INTERVAL = 0.4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{random.randbytes(2).hex()}"


class RunError(Exception):
    """Bad launch request (surfaced as 400 with a plain message)."""


class RunManager:
    def __init__(self, store: Store, repo_root: Path,
                 harness_script: Path | None = None):
        self.store = store
        self.repo_root = repo_root
        self.harness_script = harness_script  # tests: canned script, no LLM
        self.runs_root = store.root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self._guard = threading.Lock()
        self._watchers: dict[str, threading.Thread] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._spawned_at: dict[str, float] = {}

    # -- run records -------------------------------------------------------

    def _run_dir(self, run_id: str) -> Path:
        if not re.match(r"^[0-9a-f-]{8,}$", run_id) or "/" in run_id:
            raise FileNotFoundError(run_id)
        return self.runs_root / run_id

    def _read(self, run_id: str) -> dict:
        path = self._run_dir(run_id) / "run.json"
        if not path.exists():
            raise FileNotFoundError(run_id)
        return json.loads(path.read_text())

    def _write(self, run: dict) -> None:
        path = self._run_dir(run["id"]) / "run.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(run, indent=2) + "\n")
        os.replace(tmp, path)

    def get(self, run_id: str) -> dict:
        return self._read(run_id)

    def list(self) -> list[dict]:
        runs = [self._read(run_id.name) for run_id in self.runs_root.iterdir()
                if (run_id / "run.json").exists()]
        order = {"running": 0, "cancelling": 0, "converged": 1, "failed": 1,
                 "cancelled": 1, "orphaned": 1}
        runs.sort(key=lambda r: (order.get(r.get("status"), 2),
                                 r.get("created_at", ""),))
        return runs

    def active(self) -> dict | None:
        for run in self.list():
            if run.get("status") in ACTIVE_STATES:
                return run
        return None

    # -- launching -----------------------------------------------------------

    def create(self, account: dict, statement_ids: list[str], mode: str,
               key: str | None = None, rounds: int | None = None,
               max_probes: int | None = None, workdir: Path | None = None,
               ) -> dict:
        """Launch (workdir given: attach to an existing harness run).
        Validation raises before anything is created; the one-run lock is
        taken for the check-and-create window only."""
        for sid in statement_ids:
            if not self.store.statement_exists(account["slug"], sid):
                raise FileNotFoundError(f"no statement {sid!r}")
        if mode not in ("draft", "maintain"):
            raise RunError("mode must be 'draft' or 'maintain'")
        if mode == "maintain":
            if not key:
                raise RunError("maintain mode needs a key (the parser being "
                               "evolved)")
            if key not in sl.keys():
                raise RunError(f"{key!r} is not a registered parser key")

        with self._guard:
            active = self.active()
            if active is not None:
                raise RunError(f"run {active['id']} is already active — one "
                               "at a time (bounded LLM spend); cancel it "
                               "first")
            run_id = _new_run_id()
            directory = self.runs_root / run_id
            directory.mkdir(parents=True)
            events_path = directory / "events.jsonl"
            attach = workdir is not None
            run = {"id": run_id, "account_slug": account["slug"],
                   "statement_ids": statement_ids, "mode": mode, "key": key,
                   "rounds": rounds, "max_probes": max_probes,
                   "argv": None, "pid": None, "status": "running",
                   "attach": attach,
                   "workdir": str(workdir) if attach else None,
                   "created_at": _now(), "ended_at": None, "exit_code": None,
                   "auto_parse": None}
            if not attach:
                self._spawn(run, events_path)
            self._write(run)
        self._start_watcher(run)
        return run

    def _spawn(self, run: dict, events_path: Path) -> None:
        argv = [sys.executable, "tools/distill_harness.py"]
        argv += [str(self.store.original_pdf(run["account_slug"], sid))
                 for sid in run["statement_ids"]]
        argv += ["--jsonl-trajectory", str(events_path)]
        if run["rounds"]:
            argv += ["--rounds", str(run["rounds"])]
        if run["key"]:
            argv += ["--key", run["key"]]
        if run["max_probes"]:
            argv += ["--max-probes", str(run["max_probes"])]
        if self.harness_script is not None:  # tests: canned, no LLM
            argv += ["--script", str(self.harness_script), "--no-save"]
        run["argv"] = argv
        log = (self.runs_root / run["id"] / "harness.log").open("w")
        process = subprocess.Popen(  # noqa: S603 - fixed argv, our repo
            argv, cwd=self.repo_root, stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"})  # 'workdir:' must
        # reach the log NOW, not when the block buffer fills — the manager
        # resolves round artifacts from it while the run is live
        run["pid"] = process.pid
        self._spawned_at[run["id"]] = time.time()  # WALL clock — compared
        # against directory mtimes in the workdir fallback
        self._processes[run["id"]] = process  # poll() reaps; zombies don't

    # -- events -----------------------------------------------------------

    def read_events(self, run_id: str, after: int = 0) -> list[tuple[int, dict]]:
        """All events with sequence number > after, as (seq, event)."""
        path = self._run_dir(run_id) / "events.jsonl"
        if not path.exists():
            return []
        events = []
        with path.open() as handle:
            for seq, line in enumerate(handle, start=1):
                if seq <= after or not line.strip():
                    continue
                try:
                    events.append((seq, json.loads(line)))
                except ValueError:
                    continue  # a torn tail line: the writer flushes per line
        return events

    def round_detail(self, run_id: str, number: int) -> dict | None:
        run = self._read(run_id)
        workdir = run.get("workdir")
        if not workdir:
            return None
        path = Path(workdir) / f"round_{number}.py"
        if not path.exists():
            path = Path(workdir) / "baseline.py"
            if number or not path.exists():
                return None
        code = path.read_text()
        kind = ("probe" if code.lstrip().splitlines()[:1] == ["# probe"]
                else "script")
        verdict = None
        for _, event in self.read_events(run_id):
            if event.get("type") == "gate" and event.get("round") == number:
                verdict = event.get("verdict")
        return {"round": number, "kind": kind, "code": code,
                "verdict": verdict}

    # -- cancel ------------------------------------------------------------

    def cancel(self, run_id: str) -> dict:
        run = self._read(run_id)
        if run.get("status") not in ACTIVE_STATES:
            return run
        run["status"] = "cancelling"
        self._write(run)
        pid = run.get("pid")
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            threading.Timer(  # grace over: hard kill
                KILL_GRACE_SECONDS, self._hard_kill, args=(pid,)).start()
        else:
            run["status"] = "cancelled"  # attach run: nothing to signal
            run["ended_at"] = _now()
            self._write(run)
        return run

    def _hard_kill(self, pid: int) -> None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    # -- watcher ------------------------------------------------------------

    def _start_watcher(self, run: dict) -> None:
        thread = threading.Thread(target=self._watch, args=(run["id"],),
                                  daemon=True, name=f"run-watch-{run['id']}")
        self._watchers[run["id"]] = thread
        thread.start()

    def adopt(self) -> None:
        """On server restart: re-watch live runs, orphan the dead ones."""
        for run in self.list():
            if run.get("status") not in ACTIVE_STATES:
                continue
            pid = run.get("pid")
            if pid and not _pid_alive(pid):
                run["status"] = "orphaned"
                run["ended_at"] = _now()
                self._write(run)
                continue
            self._start_watcher(run)

    def _watch(self, run_id: str) -> None:
        """Tail events.jsonl + the process until a terminal state; on
        convergence auto-parse the pending statements with the new key."""
        try:
            self._watch_loop(run_id)
        finally:
            self._processes.pop(run_id, None)

    def _watch_loop(self, run_id: str) -> None:
        attach_state = {"done": False, "failed": False}
        while True:
            run = self._read(run_id)
            if run.get("status") in TERMINAL_STATES:
                return
            events = self.read_events(run_id)
            terminal = self._apply_events(run, events)
            if run.get("attach"):
                terminal = self._derive_attach(run, attach_state) or terminal
            pid = run.get("pid")
            if terminal:
                self._finish(run, terminal)
                return
            # Exited? Our own child answers via poll() (which also reaps it —
            # kill(pid, 0) succeeds on zombies, so pid-probing a child lies);
            # an adopted run (post-restart) can only be pid-probed.
            process = self._processes.get(run_id)
            exit_code = process.poll() if process is not None else None
            dead = ((process is not None and exit_code is not None)
                    or (process is None and pid
                        and not _pid_alive(pid)))
            if dead:
                # allow a brief final flush of events.jsonl
                time.sleep(0.4)
                self._apply_events(run, self.read_events(run_id))
                if run.get("status") in TERMINAL_STATES:
                    return
                if run["status"] == "cancelling" or exit_code in (130, -15,
                                                                  -9):
                    self._finish(run, "cancelled", exit_code)
                else:
                    self._finish(run, "failed", exit_code)
                return
            time.sleep(WATCH_INTERVAL)

    def _apply_events(self, run: dict, events: list[tuple[int, dict]]) -> str:
        """Fold new events into run state; return a terminal status or ''."""
        terminal = ""
        for _, event in events:
            kind = event.get("type")
            if kind == "converged":
                run["converged_key"] = event.get("key")
                run["saved"] = event.get("saved", True)
                terminal = "converged"
            elif kind in ("failed", "cancelled"):
                terminal = kind
        if terminal and run.get("status") not in TERMINAL_STATES:
            self._finish(run, terminal)
            # auto-parse whenever the converged key is loadable from the
            # registry (a --no-save draft that never registered cannot be)
            if (terminal == "converged"
                    and run.get("converged_key") in sl.keys()):
                self._auto_parse(run)
        if run.get("pid"):
            self._find_workdir(run)  # cheap; the log line is authoritative
        return terminal if run.get("status") in TERMINAL_STATES else ""

    def _find_workdir(self, run: dict) -> None:
        """The harness prints 'workdir: logs/distill_...' early (its stdout
        is unbuffered); round artifacts live there. That line is the only
        authority — it OVERWRITES any provisional fallback value, so a
        mis-guess can never stick."""
        log = self._run_dir(run["id"]) / "harness.log"
        if log.exists():
            match = re.search(r"^workdir: (.+)$",
                              log.read_text(errors="replace"), re.M)
            if match:
                truth = match.group(1).strip()
                if run.get("workdir") != truth:
                    run["workdir"] = truth
                    self._write(run)
                return
        if run.get("workdir"):
            return  # provisional value stands until the log speaks
        since = self._spawned_at.get(run["id"])
        if since is None:
            return
        candidates = [path for path in (self.repo_root / "logs").glob(
            "distill_*") if path.is_dir()
            and path.stat().st_mtime >= since - 1]
        if candidates:
            run["workdir"] = str(max(candidates,
                                     key=lambda p: p.stat().st_mtime))
            self._write(run)

    def _derive_attach(self, run: dict, state: dict) -> str:
        """Fallback (b): derive trajectory events from an existing workdir's
        artifacts — round_N.py files, GATE: lines, the trace's result."""
        workdir = run.get("workdir")
        if not workdir or not Path(workdir).exists():
            return ""
        events_path = self._run_dir(run["id"]) / "events.jsonl"
        existing = {event.get("round") for _, event in self.read_events(run["id"])
                    if event.get("type") == "round_saved"}
        with events_path.open("a") as out:
            for path in sorted(Path(workdir).glob("round_*.py"),
                               key=lambda p: int(p.stem.split("_")[1])):
                number = int(path.stem.split("_")[1])
                if number in existing:
                    continue
                code = path.read_text()
                out.write(json.dumps({
                    "type": "round_saved", "ts": _now(), "round": number,
                    "kind": ("probe" if code.lstrip().splitlines()[:1]
                             == ["# probe"] else "script"),
                    "path": path.name}) + "\n")
                gate_out = Path(workdir) / f"gate_round_{number}.out"
                if gate_out.exists():
                    match = re.search(r"^GATE:(\{.*\})$", gate_out.read_text(
                        errors="replace"), re.M)
                    if match:
                        out.write(json.dumps({
                            "type": "gate", "ts": _now(), "round": number,
                            "verdict": json.loads(match.group(1))}) + "\n")
                out.flush()
        trace = Path(workdir) / "trace.log"
        if trace.exists():
            text = trace.read_text(errors="replace")
            if "VERIFIED parser saved" in text or "VERIFIED (not saved" in text:
                state["done"], state["failed"] = True, False
                key_match = re.search(r"VERIFIED parser saved: .*/(\S+)\.py",
                                      text)
                run["converged_key"] = (key_match.group(1)
                                        if key_match
                                        else _key_from_workdir(Path(workdir))
                                        or run.get("key"))
                with events_path.open("a") as out:  # terminal event for SSE
                    out.write(json.dumps({"type": "converged", "ts": _now(),
                                          "key": run["converged_key"]}) + "\n")
                return "converged"
            if re.search(r"\[.*\] result\n-+\nFAILED", text):
                state["done"], state["failed"] = True, True
                with events_path.open("a") as out:
                    out.write(json.dumps({"type": "failed", "ts": _now(),
                                          "reason": "run finished "
                                          "(derived from trace.log)"}) + "\n")
                return "failed"
        return ""

    def _finish(self, run: dict, status: str, exit_code: int | None = None):
        run["status"] = status
        run["ended_at"] = _now()
        if exit_code is not None:
            run["exit_code"] = exit_code
        self._write(run)

    def _auto_parse(self, run: dict) -> None:
        """Converged with a saved parser: parse the pending statements with
        the new key (registry pickup needs no restart) and record outcomes."""
        key = run.get("converged_key")
        if not key:
            return
        outcomes = []
        slug = run["account_slug"]
        for sid in run["statement_ids"]:
            with self.store.account_lock(slug):
                result = parsing.probe(self.store.original_pdf(slug, sid),
                                       [key])
                if result["ok"]:
                    meta = self.store.save_parsed(slug, sid, result["stmt"],
                                                  key, sl.__version__)
                    outcomes.append({"id": sid, "status": "parsed",
                                     "transactions": (meta["summary"] or {})
                                     .get("transactions")})
                else:
                    self.store.save_failed(slug, sid, result["attempts"],
                                           result["note"])
                    outcomes.append({"id": sid, "status": "failed"})
        run["auto_parse"] = outcomes
        self._write(run)


def _key_from_workdir(workdir: Path) -> str | None:
    """KEY = "…" out of the workdir's converged artifacts (verified.py from a
    --no-save run, or the newest round script)."""
    for name in ("verified.py",):
        path = workdir / name
        if path.exists():
            match = re.search(r'^KEY\s*=\s*["\']([^"\']+)["\']',
                              path.read_text(errors="replace"), re.M)
            if match:
                return match.group(1)
    return None


def _pid_alive(pid: int) -> bool:
    """Only meaningful for processes we did NOT spawn (adopted runs):
    kill(pid, 0) succeeds on zombies too, so a child of THIS process must be
    polled through its Popen instead."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
