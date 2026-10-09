"""statementledger web UI — local, single-user, zero-database.

A thin, honest wrapper around the statementledger library (docs/web-ui-design.md):
accounts are folders, uploads are content-addressed statement folders, parsing
goes through the registry (same bank -> same parser, zero LLM), and hard
verification failures are first-class UI citizens with per-parser attempts.

Run from the repo root:

    uv sync --group web
    uv run --group web uvicorn web.app:app --host 127.0.0.1 --port 8765

Deliberately localhost-only with no auth (§10): statements are sensitive
financial data — do not port-forward, do not run on a shared host.

Response convention (§4): routes return HTML fragments for htmx and JSON when
the request Accepts it; protocol errors use real status codes (htmx is
configured in base.html to swap 4xx fragments); domain outcomes — "parsed",
"no parser verified", "duplicate" — carry a status in the body.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path

import statementledger as sl
from fastapi import (FastAPI, File, Form, HTTPException, Request,
                     UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

from web import ledger, pages, parsing
from web.runs import RunError, RunManager, TERMINAL_STATES
from web.store import AccountExists, DuplicateStatement, Store

MAX_MB_DEFAULT = 25


def _duration(started: str | None, ended: str | None) -> str:
    """Human elapsed between two ISO stamps ("2m 41s"); blank until ended."""
    if not started or not ended:
        return ""
    from datetime import datetime

    begin = datetime.fromisoformat(started)
    finish = datetime.fromisoformat(ended)
    seconds = int((finish - begin).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def _description(key: str) -> str:
    """First docstring line of a registry module, for humans (distilled
    modules often carry no docstring — that is fine, not a load failure)."""
    try:
        lines = (sl.load_module(key).__doc__ or "").strip().splitlines()
        return (lines[0] if lines else "").rstrip(" —-")
    except Exception:  # noqa: BLE001 - a broken module still gets a row
        return "(module failed to load)"


def _prov_pages(ledger: dict | None, frames: dict | None) -> list[dict]:
    """Provenance overlay geometry (§9.5): one entry per page with highlight
    bands for the anchors and every transaction that carries line info, in
    CSS pixels at the raster scale. Empty when the parser adopted no
    provenance convention."""
    if not ledger or not frames:
        return []
    lines = frames["lines"]
    bands = []

    def band(line_no: int, kind: str, index: int, label: str) -> None:
        if not isinstance(line_no, int) or not 0 <= line_no < len(lines):
            return
        line = lines[line_no]
        items = line.get("items") or []
        if not items:
            return
        x1 = min(item[1] for item in items)
        x2 = max(item[2] for item in items)
        top, bottom = line["y"] - 11, line["y"] + 2
        bands.append({"page": line["page"], "kind": kind, "index": index,
                      "label": label,
                      "left": x1 * pages.SCALE, "top": top * pages.SCALE,
                      "width": (x2 - x1) * pages.SCALE,
                      "height": (bottom - top) * pages.SCALE})

    for name, line_no in (ledger.get("_anchors") or {}).items():
        band(line_no, "anchor", -1, name)
    for index, txn in enumerate(ledger["transactions"]):
        if txn.get("line") is not None:
            band(txn["line"], "txn", index,
                 str(txn.get("description", ""))[:60])
    grouped: dict[int, list] = {}
    for entry in bands:
        grouped.setdefault(entry["page"], []).append(entry)
    return [{"page": page, "bands": page_bands}
            for page, page_bands in sorted(grouped.items())]


_llm_env_loaded = False


def _load_llm_env(repo_root: Path) -> None:
    """Load the repo's .env (exports win) — lazily, once per process; the
    launch dialog wants the model name and a real run needs the endpoint."""
    global _llm_env_loaded
    if _llm_env_loaded:
        return
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "envloader", repo_root / "tools" / "envloader.py")
    envloader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(envloader)
    envloader.load_env()
    _llm_env_loaded = True


def create_app(data_root: Path | None = None,
               max_mb: float | None = None,
               harness_script: Path | None = None) -> FastAPI:
    """harness_script: run this canned script instead of the LLM loop — the
    test seam for the whole runs pipeline (no LLM, real subprocess)."""
    store = Store(Path(data_root or os.environ.get("STMTLEDGER_WEB_DATA",
                                                   "data")))
    max_bytes = int((max_mb or float(os.environ.get("STMTLEDGER_WEB_MAX_MB",
                                                    MAX_MB_DEFAULT)))
                    * 1024 * 1024)
    repo_root = Path(__file__).resolve().parents[1]
    runs = RunManager(store, repo_root, harness_script=harness_script)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runs.adopt()  # re-watch live runs, orphan the dead ones (§8.2)
        yield

    app = FastAPI(title="statementledger", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    here = Path(__file__).parent
    env = Environment(loader=FileSystemLoader(here / "templates"),
                      autoescape=True)
    env.globals["registry_count"] = lambda: len(sl.keys())
    env.filters["money"] = lambda v: "—" if v is None else f"{v:,.2f}"
    env.filters["signed"] = (
        lambda v: "—" if v is None
        else (f"+ {v:,.2f}" if v >= 0 else f"- {abs(v):,.2f}"))
    env.filters["dt"] = lambda iso: (iso or "")[:16].replace("T", " ")
    env.filters["mask"] = (
        lambda s: "····" + s[-4:] if s and len(s) > 4 else (s or ""))
    env.filters["period"] = (
        lambda p: "—" if not p
        else p["start"][:7] if p["start"][:7] == p["end"][:7]
        else f'{p["start"]} → {p["end"]}')

    def render(template: str, request: Request, status: int = 200,
               headers: dict | None = None, **context) -> HTMLResponse:
        html = env.get_template(template).render(request=request, **context)
        return HTMLResponse(html, status_code=status, headers=headers)

    def wants_json(request: Request) -> bool:
        return ("application/json" in request.headers.get("accept", "")
                and not request.headers.get("hx-request"))

    # Page-worthy errors render as HTML for browsers, JSON for API clients.
    @app.exception_handler(HTTPException)
    def http_error(request: Request, exc: HTTPException):
        if wants_json(request):
            return JSONResponse({"detail": exc.detail},
                                status_code=exc.status_code)
        return render("error.html", request, status=exc.status_code,
                      code=exc.status_code, message=exc.detail)

    def require_account(slug: str) -> dict:
        account = store.get_account(slug)
        if account is None:
            raise HTTPException(404, f"no account {slug!r}")
        return account

    # -- account rows (shared by / and the create-account error path) --------

    def _account_rows() -> list[dict]:
        rows = []
        for account in store.accounts():
            metas = store.statements(account["slug"])
            parsed = [m for m in metas if m["status"] == "parsed"]
            latest = parsed[-1] if parsed else None
            rows.append({
                "account": account, "metas": metas, "parsed": len(parsed),
                "failed": sum(1 for m in metas if m["status"] == "failed"),
                "latest": latest,
                "ledger": (store.statement_ledger(account["slug"],
                                                  latest["id"])
                           if latest else None),
                "chain": ledger.chain(metas, sl.TOL),
                "updated": max((m["uploaded_at"] for m in metas),
                               default=None)})
        rows.sort(key=lambda r: r["account"]["display_name"].lower())
        return rows

    def _latest_ledger(slug: str) -> dict | None:
        for meta in reversed(store.statements(slug)):
            if meta["status"] == "parsed":
                return store.statement_ledger(slug, meta["id"])
        return None

    # -- pages ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return render("accounts.html", request, accounts=_account_rows(),
                      keys=sl.keys(), error=None)

    @app.post("/accounts")
    def create_account(request: Request, display_name: str = Form(""),
                       registry_key: str = Form("")):

        def fail(status: int, message: str):
            if wants_json(request):
                return JSONResponse({"detail": message}, status_code=status)
            return render("accounts.html", request, status=status,
                          accounts=_account_rows(), keys=sl.keys(),
                          error=message)

        name = display_name.strip()
        if not name:
            return fail(400, "Display name is required.")
        key = registry_key.strip() or None
        if key is not None and key not in sl.keys():
            return fail(400, f"{key!r} is not a registered parser key.")
        try:
            account = store.create_account(name, key, "user-pinned")
        except AccountExists:
            return fail(409, f"An account already exists for {name!r}.")
        except ValueError as error:  # name yields no usable slug
            return fail(400, str(error))
        target = f"/accounts/{account['slug']}"
        if request.headers.get("hx-request"):
            return Response(status_code=200, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    @app.get("/accounts/{slug}", response_class=HTMLResponse)
    def account_detail(slug: str, request: Request):
        account = require_account(slug)
        metas = store.statements(slug)
        _load_llm_env(repo_root)
        return render("account.html", request, account=account, metas=metas,
                      ledger=_latest_ledger(slug), keys=sl.keys(),
                      outcome=None, error=None,
                      chain=ledger.chain(metas, sl.TOL),
                      warnings=ledger.period_warnings(metas),
                      runs=_account_runs(slug),
                      llm_model=os.environ.get(
                          "STATEMENTLEDGER_LLM_MODEL", ""))

    @app.delete("/accounts/{slug}")
    def delete_account(slug: str, request: Request):
        require_account(slug)
        store.delete_account(slug)
        return Response(status_code=204, headers={"HX-Redirect": "/"})

    @app.post("/accounts/{slug}/key")
    def set_key(slug: str, request: Request, registry_key: str = Form("")):
        account = require_account(slug)

        def fail(status: int, message: str):
            if wants_json(request):
                return JSONResponse({"detail": message}, status_code=status)
            metas = store.statements(slug)
            return render("account.html", request, status=status,
                          account=account, metas=metas,
                          ledger=_latest_ledger(slug), keys=sl.keys(),
                          outcome=None, error=message,
                          chain=ledger.chain(metas, sl.TOL),
                          warnings=ledger.period_warnings(metas))

        key = registry_key.strip() or None
        if key is not None and key not in sl.keys():
            return fail(400, f"{key!r} is not a registered parser key.")
        account["registry_key"] = key
        account["registry_key_source"] = "user-pinned" if key else None
        store.update_account(account)
        target = f"/accounts/{slug}"
        if request.headers.get("hx-request"):
            return Response(status_code=200, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    # -- statements ----------------------------------------------------------

    def _pdf_rel(slug: str, sid: str) -> str:
        return f"data/accounts/{slug}/statements/{sid}/original.pdf"

    def _run_parse(account: dict, sid: str, key_override: str | None):
        """Parse a stored PDF with the account pin (or an override), persist
        the outcome, and build the fragment/JSON outcome dict."""
        candidates = ([key_override] if key_override
                      else parsing.candidates_for(account))
        result = parsing.probe(store.original_pdf(account["slug"], sid),
                               candidates)
        if result["ok"]:
            meta = store.save_parsed(account["slug"], sid, result["stmt"],
                                     result["key"], sl.__version__)
            outcome = {"kind": "parsed", "statement": meta,
                       "key": result["key"]}
            if not account.get("registry_key"):  # detection mode (§7.2)
                if len(result["verified_keys"]) == 1:
                    outcome["suggest_pin"] = result["key"]
                elif len(result["verified_keys"]) > 1:
                    outcome["ambiguous"] = result["verified_keys"]
        else:
            meta = store.save_failed(account["slug"], sid, result["attempts"],
                                     result["note"])
            outcome = {"kind": "failed", "statement": meta,
                       "attempts": result["attempts"], "note": result["note"],
                       "pdf_rel": _pdf_rel(account["slug"], sid),
                       "pinned": bool(account.get("registry_key"))}
        return outcome

    def _statements_region(request: Request, slug: str, outcome: dict,
                           status: int):
        if wants_json(request):
            if "report" in outcome:  # re-parse-all: one report, not per-file
                return JSONResponse(
                    {"status": "ok", "changed": outcome["changed"],
                     "results": outcome["report"]}, status_code=status)
            statement = outcome["statement"]
            body = {"status": outcome["kind"], "id": statement["id"],
                    "period": statement["period"],
                    "summary": statement["summary"]}
            for extra in ("key", "attempts", "note", "suggest_pin",
                          "ambiguous"):
                if outcome.get(extra) is not None:
                    body[extra] = outcome[extra]
            return JSONResponse(body, status_code=status)
        metas = store.statements(slug)
        _load_llm_env(repo_root)
        return render("statements_region.html", request, status=status,
                      account=store.get_account(slug), metas=metas,
                      outcome=outcome, ledger=_latest_ledger(slug),
                      warnings=ledger.period_warnings(metas),
                      chain=ledger.chain(metas, sl.TOL),
                      llm_model=os.environ.get(
                          "STATEMENTLEDGER_LLM_MODEL", ""))

    @app.post("/accounts/{slug}/statements")
    def upload_statement(slug: str, request: Request,
                         file: UploadFile | None = File(None)):
        account = require_account(slug)

        def error(status: int, message: str):
            if wants_json(request):
                return JSONResponse({"detail": message}, status_code=status)
            metas = store.statements(slug)
            return render("statements_region.html", request, status=status,
                          account=account, metas=metas,
                          outcome={"kind": "error", "message": message},
                          ledger=_latest_ledger(slug),
                          warnings=ledger.period_warnings(metas),
                          chain=ledger.chain(metas, sl.TOL))

        if file is None or not file.filename:
            return error(400, "No file was uploaded.")
        content = file.file.read()
        if not content:
            return error(400, "The uploaded file is empty.")
        if len(content) > max_bytes:
            return error(413, f"PDF exceeds the {max_bytes // (1024 * 1024)} "
                              "MiB upload limit.")
        if b"%PDF-" not in content[:1024]:
            return error(400, "Not a PDF (magic bytes missing) — statements "
                              "must be PDF files.")
        digest = hashlib.sha256(content).hexdigest()
        sid = digest[:16]
        with store.account_lock(slug):
            try:
                store.store_upload(slug, content, sid, digest, file.filename)
            except DuplicateStatement:
                outcome = {"kind": "duplicate",
                           "statement": store.statement_meta(slug, sid)}
                return _statements_region(request, slug, outcome, 409)
            outcome = _run_parse(account, sid, None)
        return _statements_region(request, slug, outcome,
                                  201 if outcome["kind"] == "parsed" else 200)

    @app.post("/accounts/{slug}/statements/{sid}/parse")
    def reparse_statement(slug: str, sid: str, request: Request,
                          key: str = Form(""), back_to: str = Form("")):
        account = require_account(slug)
        if not store.statement_exists(slug, sid):
            raise HTTPException(404, f"no statement {sid!r} in {slug!r}")
        key_override = key.strip() or None
        if key_override and key_override not in sl.keys():
            raise HTTPException(400, f"{key_override!r} is not a registered "
                                     "parser key.")
        with store.account_lock(slug):
            account = store.get_account(slug)  # re-read under the lock
            outcome = _run_parse(account, sid, key_override)
        # A plain form post (statement page) redirects back to its page.
        if (back_to.startswith("/") and not wants_json(request)
                and not request.headers.get("hx-request")):
            return RedirectResponse(back_to, status_code=303)
        return _statements_region(request, slug, outcome, 200)

    @app.get("/accounts/{slug}/statements/{sid}", response_class=HTMLResponse)
    def statement_detail(slug: str, sid: str, request: Request):
        account = require_account(slug)
        meta = store.statement_meta(slug, sid)
        if meta is None:
            raise HTTPException(404, f"no statement {sid!r} in {slug!r}")
        ledger = store.statement_ledger(slug, sid)
        verified = sl.reconcile_statement(ledger) is None if ledger else None
        _load_llm_env(repo_root)
        # §9.5 stretch: parsers MAY adopt a provenance convention
        # (txn["line"], stmt["_anchors"]); without it there is no overlay.
        provenance = None
        has_provenance = bool(ledger) and (
            ledger.get("_anchors")
            or any(txn.get("line") is not None
                   for txn in ledger["transactions"]))
        if has_provenance:
            try:
                provenance = pages.cached_frames(
                    store.original_pdf(slug, sid), store.root / "cache")
            except Exception:  # noqa: BLE001 - overlay degrades gracefully
                provenance = None
        return render("statement.html", request, account=account, meta=meta,
                      ledger=ledger, verified=verified,
                      pdf_rel=_pdf_rel(slug, sid),
                      prov_pages=_prov_pages(ledger, provenance),
                      llm_model=os.environ.get(
                          "STATEMENTLEDGER_LLM_MODEL", ""))

    @app.get("/accounts/{slug}/statements/{sid}/original.pdf")
    def original_pdf(slug: str, sid: str, request: Request):
        require_account(slug)
        path = store.original_pdf(slug, sid)
        if not path.exists():
            raise HTTPException(404, f"no statement {sid!r} in {slug!r}")
        meta = store.statement_meta(slug, sid) or {}
        return FileResponse(path, media_type="application/pdf",
                            filename=meta.get("original_filename",
                                              "statement.pdf"))

    @app.delete("/accounts/{slug}/statements/{sid}")
    def delete_statement(slug: str, sid: str, request: Request):
        require_account(slug)
        store.delete_statement(slug, sid)
        return Response(status_code=204)

    # -- probe / re-parse-all / exports (Phase 2, §7.3–§7.4) -----------------

    @app.post("/accounts/{slug}/probe")
    async def probe(slug: str, request: Request):
        """Dry-run try-all: an uploaded PDF or an already-stored statement,
        nothing persisted (§7.3). Answers 'what would happen?' — per-key
        verdicts plus the verified set (a picker when ambiguous)."""
        account = require_account(slug)
        sid = ""
        content = b""
        if request.headers.get("content-type", "").startswith(
                "application/json"):
            payload = await request.json()
            sid = str(payload.get("statement_id") or "")
        else:
            form = await request.form()
            sid = str(form.get("statement_id") or "")
            upload = form.get("file")
            if isinstance(upload, StarletteUploadFile):
                content = await upload.read()
        if not content and not sid:
            raise HTTPException(400, "provide a file or a statement_id")
        if content:
            if not content or len(content) > max_bytes:
                raise HTTPException(413, "file empty or over the size limit")
            if b"%PDF-" not in content[:1024]:
                raise HTTPException(400, "not a PDF (magic bytes missing)")
            with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                tmp.write(content)
                tmp.flush()
                outcome = await run_in_threadpool(parsing.probe_all,
                                                  Path(tmp.name))
        else:
            if not store.statement_exists(slug, sid):
                raise HTTPException(404, f"no statement {sid!r} in {slug!r}")
            outcome = await run_in_threadpool(
                parsing.probe_all, store.original_pdf(slug, sid))
        verified = outcome["verified_keys"]
        suggested = verified[0] if len(verified) == 1 else None
        if wants_json(request):
            return {"results": outcome["results"],
                    "verified_count": len(verified),
                    "suggested_key": suggested, "note": outcome["note"]}
        return render("probe.html", request, account=account,
                      results=outcome["results"], verified=verified,
                      suggested=suggested, note=outcome["note"])

    @app.post("/accounts/{slug}/reparse")
    def reparse_all(slug: str, request: Request):
        """Re-parse every stored statement with the pinned key; each entry
        reports old→new (status, period, summary) and any drift is flagged
        loudly (§7.4)."""
        account = require_account(slug)
        if not account.get("registry_key"):
            raise HTTPException(400, "pin a parser key first (the account is "
                                     "in detection mode)")
        report = []
        with store.account_lock(slug):
            account = store.get_account(slug)  # re-read under the lock
            key = account["registry_key"]
            for meta in store.statements(slug):
                sid = meta["id"]
                result = parsing.probe(store.original_pdf(slug, sid), [key])
                if result["ok"]:
                    new_meta = store.save_parsed(slug, sid, result["stmt"],
                                                 key, sl.__version__)
                else:
                    new_meta = store.save_failed(slug, sid,
                                                 result["attempts"],
                                                 result["note"])
                report.append(ledger.drift(meta, new_meta))
        outcome = {"kind": "report", "key": key, "report": report,
                   "changed": any(entry["changed"] for entry in report)}
        return _statements_region(request, slug, outcome, 200)

    @app.get("/accounts/{slug}/statements/{sid}/ledger.json")
    def statement_ledger_json(slug: str, sid: str, request: Request):
        require_account(slug)
        stmt = store.statement_ledger(slug, sid)
        if stmt is None:
            raise HTTPException(409, "statement not parsed yet")
        return JSONResponse(stmt)

    @app.get("/accounts/{slug}/statements/{sid}/ledger.csv")
    def statement_ledger_csv(slug: str, sid: str, request: Request):
        require_account(slug)
        stmt = store.statement_ledger(slug, sid)
        if stmt is None:
            raise HTTPException(409, "statement not parsed yet")
        return Response(ledger.to_csv(stmt), media_type="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{sid}.csv"'})

    @app.get("/accounts/{slug}/ledger.json")
    def account_ledger_json(slug: str, request: Request):
        """Merged ledger across every parsed statement (transactions in
        (date, statement) order, each tagged with its statement id)."""
        account = require_account(slug)
        metas = store.statements(slug)
        ledgers = {m["id"]: store.statement_ledger(slug, m["id"])
                   for m in metas if m["status"] == "parsed"}
        return JSONResponse(ledger.merged(account, metas, ledgers))

    # -- registry ------------------------------------------------------------

    def _registry_rows() -> list[dict]:
        accounts = store.accounts()
        parsed_counts: Counter[str] = Counter()
        for account in accounts:
            for meta in store.statements(account["slug"]):
                if meta["status"] == "parsed" and meta["history"]:
                    parsed_counts[meta["history"][-1].get("key", "")] += 1
        return [{"key": key, "description": _description(key),
                 "accounts": sum(1 for a in accounts
                                 if a["registry_key"] == key),
                 "statements": parsed_counts[key]}
                for key in sl.keys()]

    @app.get("/registry", response_class=HTMLResponse)
    def registry_page(request: Request):
        return render("registry.html", request, rows=_registry_rows())

    @app.get("/registry.json")
    def registry_json():
        return [{"key": row["key"], "description": row["description"]}
                for row in _registry_rows()]

    # -- PDF pages + frames (overlay geometry, §9.3) --------------------------

    def _statement_paths(slug: str, sid: str) -> tuple[dict, Path]:
        account = require_account(slug)
        if not store.statement_exists(slug, sid):
            raise HTTPException(404, f"no statement {sid!r} in {slug!r}")
        return account, store.original_pdf(slug, sid)

    @app.get("/accounts/{slug}/statements/{sid}/pages/{page}.png")
    def statement_page_png(slug: str, sid: str, page: int, request: Request):
        _, pdf = _statement_paths(slug, sid)
        try:
            png = pages.page_png(pdf, page, store.root / "cache")
        except Exception as error:  # noqa: BLE001 - damaged PDFs, as data
            raise HTTPException(422, f"could not render page {page}: "
                                     f"{type(error).__name__}: {error}")
        if png is None:
            raise HTTPException(404, f"page {page} does not exist")
        return FileResponse(png, media_type="image/png")

    @app.get("/accounts/{slug}/statements/{sid}/frames.json")
    def statement_frames(slug: str, sid: str, request: Request):
        _, pdf = _statement_paths(slug, sid)
        try:
            return JSONResponse(pages.cached_frames(pdf, store.root / "cache"))
        except Exception as error:  # noqa: BLE001
            raise HTTPException(422, f"could not extract frames: "
                                     f"{type(error).__name__}: {error}")

    # -- distill runs (§8–§9): the one real background job ---------------------

    @app.post("/runs")
    async def launch_run(request: Request):
        """JSON {account_slug, statement_ids, mode, key?, rounds?,
        max_probes?, workdir?} or the same fields as a form. workdir set =
        attach to an existing harness run (tail-watch fallback, §8.2b)."""
        if request.headers.get("content-type", "").startswith(
                "application/json"):
            payload = await request.json()
        else:
            form = await request.form()
            payload = {key: form[key] for key in (
                "account_slug", "statement_ids", "mode", "key", "rounds",
                "max_probes", "workdir") if form.get(key)}
        slug = str(payload.get("account_slug") or "")
        account = store.get_account(slug)
        if account is None:
            raise HTTPException(404, f"no account {slug!r}")
        ids = payload.get("statement_ids") or []
        if isinstance(ids, str):
            ids = [part.strip() for part in ids.split(",") if part.strip()]
        mode = str(payload.get("mode") or "draft")
        key = str(payload.get("key") or "").strip() or None
        if mode == "maintain" and not key and account.get("registry_key"):
            key = account["registry_key"]  # maintain = evolve the pin
        rounds = payload.get("rounds")
        max_probes = payload.get("max_probes")
        workdir = str(payload.get("workdir") or "").strip() or None
        if workdir:  # attach mode: resolve + sanity-check the workdir
            candidate = (repo_root / workdir).resolve()
            if not candidate.is_relative_to(repo_root) or not (
                    candidate / "trace.log").exists():
                raise HTTPException(400, f"{workdir!r} is not a harness "
                                         "workdir (no trace.log)")
            workdir = candidate
        elif harness_script is None:  # a real LLM run needs the endpoint
            _load_llm_env(repo_root)
            if not os.environ.get("STATEMENTLEDGER_LLM_URL"):
                raise HTTPException(
                    400, "no LLM endpoint configured — set "
                    "STATEMENTLEDGER_LLM_* in .env (see .env.example)")
        try:
            run = runs.create(account, ids, mode, key=key,
                              rounds=int(rounds) if rounds else None,
                              max_probes=(int(max_probes) if max_probes
                                          else None),
                              workdir=workdir)
        except FileNotFoundError as error:
            raise HTTPException(404, str(error))
        except RunError as error:
            status = 409 if "already active" in str(error) else 400
            raise HTTPException(status, str(error))
        target = f"/runs/{run['id']}"
        if request.headers.get("hx-request"):
            return Response(status_code=200,
                            headers={"HX-Redirect": target})
        if wants_json(request):
            return JSONResponse({"run_id": run["id"]}, status_code=202)
        return RedirectResponse(target, status_code=303)

    @app.get("/runs", response_class=HTMLResponse)
    def runs_list(request: Request):
        items = runs.list()
        for run in items:  # human names + duration for the table
            account = store.get_account(run["account_slug"])
            run["display_name"] = (account or {}).get(
                "display_name", run["account_slug"])
            run["duration"] = _duration(run.get("created_at"),
                                        run.get("ended_at"))
        if wants_json(request):
            return JSONResponse(items)
        return render("runs.html", request, runs=items)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(run_id: str, request: Request):
        try:
            run = runs.get(run_id)
        except FileNotFoundError:
            raise HTTPException(404, f"no run {run_id!r}")
        if wants_json(request):
            return JSONResponse(run)
        account = store.get_account(run["account_slug"])
        first_sid = run["statement_ids"][0] if run["statement_ids"] else None
        page_total = 0
        model = ""
        for _, event in runs.read_events(run_id):
            if event.get("type") == "run_started" and event.get("model"):
                model = event["model"]
        if first_sid:
            pdf = store.original_pdf(run["account_slug"], first_sid)
            try:
                page_total = pages.page_count(pdf, store.root / "cache")
            except Exception:  # noqa: BLE001 - overlay degrades without pages
                page_total = 0
        config = {"id": run["id"], "slug": run["account_slug"],
                  "sid": first_sid, "pages": page_total}
        return render("run.html", request, run=run, account=account,
                      model=model, pages=page_total, config=config)

    @app.delete("/runs/{run_id}")
    def cancel_run(run_id: str, request: Request):
        try:
            runs.cancel(run_id)
        except FileNotFoundError:
            raise HTTPException(404, f"no run {run_id!r}")
        return Response(status_code=202)

    @app.get("/runs/{run_id}/rounds/{number}")
    def run_round(run_id: str, number: int, request: Request):
        try:
            detail = runs.round_detail(run_id, number)
        except FileNotFoundError:
            raise HTTPException(404, f"no run {run_id!r}")
        if detail is None:
            raise HTTPException(404, f"no round {number}")
        return JSONResponse(detail)

    @app.get("/runs/{run_id}/rounds/{number}/output")
    def run_round_output(run_id: str, number: int, request: Request):
        """The round's captured stdout/stderr (round_N.out) — what the probe
        or script actually printed, for the run view's trace pane."""
        try:
            run = runs.get(run_id)
        except FileNotFoundError:
            raise HTTPException(404, f"no run {run_id!r}")
        workdir = run.get("workdir")
        path = (Path(workdir) / f"round_{number}.out" if workdir else None)
        if path is None or not path.exists():
            raise HTTPException(404, f"no output for round {number}")
        return JSONResponse({"round": number,
                             "output": path.read_text(errors="replace")})

    @app.get("/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request):
        """SSE relay of the harness trajectory: one event per line, id = the
        sequence number, so reconnects (Last-Event-ID / ?after=) resume
        without loss (§9.4). A terminal `run_status` event closes the stream
        even when the harness died without writing one (SIGKILL)."""
        try:
            runs.get(run_id)
        except FileNotFoundError:
            raise HTTPException(404, f"no run {run_id!r}")
        after = request.query_params.get("after") or \
            request.headers.get("last-event-id") or "0"

        async def stream():
            seq = int(after)
            while True:
                if await request.is_disconnected():
                    return
                events = await run_in_threadpool(runs.read_events, run_id,
                                                 seq)
                for number, event in events:
                    seq = number
                    yield f"id: {seq}\ndata: {json.dumps(event)}\n\n"
                run = await run_in_threadpool(runs.get, run_id)
                if run.get("status") in TERMINAL_STATES:
                    yield (f"id: {seq + 1}\nevent: run_status\ndata: "
                           f'{json.dumps({"status": run["status"]})}\n\n')
                    return
                yield ": ping\n\n"
                await asyncio.sleep(0.4)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    # -- account page: active + recent runs -----------------------------------

    def _account_runs(slug: str) -> list[dict]:
        return [run for run in runs.list()
                if run["account_slug"] == slug][:5]

    app.mount("/static", StaticFiles(directory=here / "static"), name="static")
    return app


app = create_app()
