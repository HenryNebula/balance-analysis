"""Distill-agent run tracking (docs/web-ui-design.md §8.2, §9): the one
real background job.

The manager spawns tools/distill_agent.py as a tracked subprocess. The
agent EMITS its own trajectory: an append-only events.jsonl in the workdir
it prints as `workdir: ...` on stdout. A watcher thread per run tails that
file into data/runs/<id>/events.jsonl, and the app relays it over SSE.
Workdirs from agents before structured emission (or with only a trace.log)
fall back to web.agent_events.TraceDeriver — same event vocabulary, so the
run view renders identically. run.json is the durable record — reload-safe.
One active run at a time (bounded LLM spend). DELETE = SIGTERM to the
process group, SIGKILL after a grace period; the manager writes the
terminal `cancelled` event itself (the agent does not trap SIGTERM). A run
whose pid is gone on restart is marked `orphaned` with its artifacts still
browsable. On VERIFIED the pending statements are auto-parsed with the new
key (the registry overlay needs no restart).

Attach mode (§8.2 fallback b): instead of spawning, watch an existing
agent workdir (any dir with an events.jsonl or a trace.log) — same tailing,
replayed from byte zero; events already persisted are skipped, so a
re-attach or a post-restart adopt never duplicates.
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
from web.agent_events import TraceDeriver
from web.store import Store

ACTIVE_STATES = ("running", "cancelling")
TERMINAL_STATES = ("converged", "failed", "cancelled", "orphaned")
KILL_GRACE_SECONDS = 5.0
WATCH_INTERVAL = 0.4
ATTACH_STALL_SECONDS = 180.0  # attach run, no pid: quiet trace → failed


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
        self.registry_root = store.root / "registry"  # THIS app's overlay
        # (spawned agents save here; auto-parse loads from here)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self._guard = threading.Lock()
        self._watchers: dict[str, threading.Thread] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._tails: dict[str, dict] = {}  # run_id -> trace tail state

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
        try:
            run_dirs = list(self.runs_root.iterdir())
        except FileNotFoundError:  # runs/ vanished under a live server
            return []
        runs = [self._read(run_id.name) for run_id in run_dirs
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
               key: str | None = None, turns: int | None = None,
               workdir: Path | None = None,
               ) -> dict:
        """Launch (workdir given: attach to an existing agent run).
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
            if key not in sl.keys(self.registry_root):
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
                   "turns": turns,
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
        argv = [sys.executable, "tools/distill_agent.py"]
        argv += [str(self.store.original_pdf(run["account_slug"], sid))
                 for sid in run["statement_ids"]]
        if run["turns"]:
            argv += ["--turns", str(run["turns"])]
        if run["key"]:
            argv += ["--key", run["key"]]
        if self.harness_script is not None:  # tests: canned, no model
            argv += ["--script", str(self.harness_script)]
        run["argv"] = argv
        log = (self.runs_root / run["id"] / "harness.log").open("w")
        process = subprocess.Popen(  # noqa: S603 - fixed argv, our repo
            argv, cwd=self.repo_root, stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1",
                 # pin the overlay to THIS app's data root — the inherited
                 # env may point at another app instance's (or the dev
                 # server's) registry, and a verified parser would save there
                 "STATEMENTLEDGER_USER_REGISTRY":
                     str(self.registry_root)})  # 'workdir:' must
        # reach the log NOW, not when the block buffer fills — the manager
        # resolves the trace tail from it while the run is live
        run["pid"] = process.pid
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

    def _append_events(self, run_id: str, events: list[dict]) -> None:
        if not events:
            return
        with (self._run_dir(run_id) / "events.jsonl").open("a") as out:
            for event in events:
                out.write(json.dumps(event) + "\n")

    def _statement_lines(self, run: dict) -> list[dict] | None:
        """The first statement's frames lines (for look-event rects), or
        None when there is nothing to resolve against."""
        if not run.get("statement_ids"):
            return None
        try:
            from web import pages

            pdf = self.store.original_pdf(run["account_slug"],
                                          run["statement_ids"][0])
            return pages.cached_frames(pdf, self.store.root / "cache")["lines"]
        except Exception:  # noqa: BLE001 - look events degrade, run doesn't
            return None

    def _pump(self, run: dict, final: bool = False) -> None:
        """Fold new workdir output into events.jsonl. Current agents write
        events.jsonl themselves — tail it verbatim. Legacy workdirs (and
        anything with only a trace.log) go through the TraceDeriver. A
        fresh tail (post-restart adopt, attach) replays everything and
        skips the events already persisted."""
        workdir = run.get("workdir")
        if not workdir:
            return
        source = Path(workdir) / "events.jsonl"
        if source.exists():
            self._pump_agent_events(run, source, final)
            return
        trace = Path(workdir) / "trace.log"
        if not trace.exists():
            return
        run_id = run["id"]
        tail = self._tails.get(run_id)
        if tail is not None and tail.get("workdir") != workdir:
            # the authoritative workdir line corrected a guess: never keep
            # events derived from another run's trace
            tail = None
            self._tails.pop(run_id, None)
        if tail is None:
            deriver = TraceDeriver(self._statement_lines(run))
            staged = Path(workdir) / "parser.py"
            if deriver.parser_code == "" and staged.exists():
                # canned/baseline runs stage parser.py without a write
                # section — seed the snapshot so key_hint() can answer
                deriver.parser_code = staged.read_text(errors="replace")
            tail = {"deriver": deriver,
                    "offset": 0, "workdir": workdir,
                    "skip": len(self.read_events(run_id))}
            self._tails[run_id] = tail
        data = trace.read_text(errors="replace")[tail["offset"]:]
        if not data and not final:
            return
        tail["offset"] += len(data)
        events = tail["deriver"].feed(data)
        events += tail["deriver"].peek_terminal()
        if final:
            events += tail["deriver"].close()
        if tail["skip"]:
            events = events[tail["skip"]:]
            tail["skip"] = 0
        self._append_events(run_id, events)

    def _pump_agent_events(self, run: dict, source: Path,
                           final: bool) -> None:
        """Tail the agent-emitted events.jsonl into the run's events.jsonl,
        line-for-line. Only complete lines advance the offset — the agent
        flushes per line, so a torn tail waits for its bytes."""
        run_id = run["id"]
        workdir = str(source.parent)
        tail = self._tails.get(run_id)
        if tail is not None and tail.get("workdir") != workdir:
            tail = None  # same guard as the deriver path: another run's tail
            self._tails.pop(run_id, None)
        if tail is None or tail.get("mode") != "events":
            tail = {"mode": "events", "workdir": workdir, "offset": 0,
                    "skip": len(self.read_events(run_id))}
            self._tails[run_id] = tail
        data = source.read_bytes()[tail["offset"]:]
        if not data and not final:
            return
        complete = data[: data.rfind(b"\n") + 1] if b"\n" in data else b""
        if not complete:
            return
        tail["offset"] += len(complete)
        events = []
        for line in complete.splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue  # defensive: the agent writes valid JSON per line
        if tail["skip"]:
            events = events[tail["skip"]:]
            tail["skip"] = 0
        self._append_events(run_id, events)

    def turn_detail(self, run_id: str, number: int) -> dict | None:
        """Everything the run view shows for one turn: the parser.py
        snapshot as of that turn, its gate verdict, the turn summary, and
        the tool call/result transcript."""
        run = self._read(run_id)
        code = None
        verdict = None
        summary = None
        notes: list[str] = []
        tools: list[dict] = []
        for _, event in self.read_events(run_id):
            kind = event.get("type")
            turn = event.get("turn", 0)
            if kind == "tool" and turn == number:
                tools.append({"name": event.get("name"),
                              "args": event.get("args"),
                              "result": event.get("result", "")})
            elif kind == "parser" and turn <= number:
                code = event.get("content", "")  # events arrive in order:
                # the last snapshot at or before this turn is its state
            elif kind == "gate" and turn <= number:
                verdict = event.get("verdict")
            elif kind == "turn" and turn == number:
                summary = event.get("summary")
                notes = event.get("notes") or []
            elif kind == "chat_failed" and turn == number:
                notes.append(event.get("error", ""))
            elif kind == "elided" and turn == number:
                notes.append("context elided "
                             f"({event.get('chars', 0)} chars)")
        if code is None and verdict is None and summary is None and not tools:
            return None
        if code is None and number == 0:
            code = self._baseline_code(run)
        return {"turn": number, "code": code, "verdict": verdict,
                "summary": summary, "notes": notes, "tools": tools}

    def _baseline_code(self, run: dict) -> str | None:
        """Turn 0 = the staged parser before the loop: for maintain runs
        that is exactly the registry source of the key (the file evolves
        afterwards, so reading it later would lie); for canned drafts the
        staged parser.py in the workdir (written once, never edited)."""
        try:
            if run.get("key"):
                from statementledger.registry import REGISTRY, module_path

                path = module_path(run["key"])
                if not path.exists():
                    path = REGISTRY / f"{run['key']}.py"
                return path.read_text()
            workdir = run.get("workdir")
            if workdir:
                staged = Path(workdir) / "parser.py"
                if staged.exists():
                    return staged.read_text(errors="replace")
        except OSError:
            pass
        return None

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
            self._append_events(run_id, [{"type": "cancelled"}])
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
        """Tail trace.log → events.jsonl until a terminal state; on
        convergence auto-parse the pending statements with the new key."""
        try:
            self._watch_loop(run_id)
        finally:
            self._processes.pop(run_id, None)
            self._tails.pop(run_id, None)

    def _watch_loop(self, run_id: str) -> None:
        quiet_since: float | None = None
        while True:
            run = self._read(run_id)
            if run.get("status") in TERMINAL_STATES:
                return
            self._pump(run)
            if run.get("pid"):
                self._find_workdir(run)  # cheap; the log line is authority
            terminal = self._apply_events(run, self.read_events(run_id))
            if terminal:
                return
            # Exited? Our own child answers via poll() (which also reaps it —
            # kill(pid, 0) succeeds on zombies, so pid-probing a child lies);
            # an adopted run (post-restart) can only be pid-probed.
            process = self._processes.get(run_id)
            exit_code = process.poll() if process is not None else None
            pid = run.get("pid")
            dead = ((process is not None and exit_code is not None)
                    or (process is None and pid
                        and not _pid_alive(pid)))
            if dead:
                # allow a brief final flush of the trace
                time.sleep(0.4)
                self._pump(run, final=True)
                self._apply_events(run, self.read_events(run_id))
                if run.get("status") in TERMINAL_STATES:
                    return
                if run["status"] == "cancelling" or exit_code in (130, -15,
                                                                  -9):
                    self._append_events(run_id, [{"type": "cancelled"}])
                    self._finish(run, "cancelled", exit_code)
                else:
                    self._synthesize_terminal(run_id, run, exit_code)
                return
            if not run.get("pid"):  # attach: no process to observe
                trace = (Path(run["workdir"]) / "trace.log"
                         if run.get("workdir") else None)
                grew = bool(trace and trace.exists()
                            and trace.stat().st_mtime > time.time() - 5)
                quiet_since = time.time() if grew else (quiet_since
                                                        or time.time())
                if quiet_since and time.time() - quiet_since > ATTACH_STALL_SECONDS:
                    self._append_events(run_id, [{
                        "type": "failed",
                        "reason": "trace went quiet without a result section"}])
                    self._finish(run, "failed")
                    return
            time.sleep(WATCH_INTERVAL)

    def _synthesize_terminal(self, run_id: str, run: dict,
                             exit_code: int | None) -> None:
        """The process died without a terminal event (torn trace, crash):
        exit code 0 still means VERIFIED, anything else failed."""
        tail = self._tails.get(run_id)
        key = (tail["deriver"].key_hint()
               if tail and tail.get("mode") != "events" else None) \
            or run.get("key")
        if exit_code == 0:
            self._append_events(run_id, [{"type": "converged",
                                          "key": key, "saved": True}])
            self._finish(run, "converged", exit_code)
        else:
            self._append_events(run_id, [{"type": "failed", "reason":
                                          f"agent exited {exit_code}"}])
            self._finish(run, "failed", exit_code)

    def _apply_events(self, run: dict, events: list[tuple[int, dict]]) -> str:
        """Fold new events into run state; return a terminal status or ''."""
        terminal = ""
        for _, event in events:
            kind = event.get("type")
            if kind == "converged":
                run["converged_key"] = event.get("key") or run.get("key")
                run["saved"] = event.get("saved", True)
                terminal = "converged"
            elif kind in ("failed", "cancelled"):
                terminal = kind
        if terminal and run.get("status") not in TERMINAL_STATES:
            self._finish(run, terminal)
            # auto-parse whenever the converged key is loadable from the
            # registry overlay (an unsaved draft cannot be)
            if (terminal == "converged"
                    and run.get("converged_key")
                    in sl.keys(self.registry_root)):
                self._auto_parse(run)
        return terminal if run.get("status") in TERMINAL_STATES else ""

    def _find_workdir(self, run: dict) -> None:
        """The agent prints 'workdir: logs/distill_...' early (its stdout
        is unbuffered) — the ONLY source for spawned runs. No mtime
        guessing: a just-finished neighbour run's workdir would satisfy any
        recency window, and its trace would inject wrong events."""
        log = self._run_dir(run["id"]) / "harness.log"
        if not log.exists() or run.get("workdir"):
            return
        match = re.search(r"^workdir: (.+)$",
                          log.read_text(errors="replace"), re.M)
        if match:
            run["workdir"] = match.group(1).strip()
            self._write(run)

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
                                       [key], overlay=self.registry_root)
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
