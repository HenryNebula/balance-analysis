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

import hashlib
import os
from collections import Counter
from pathlib import Path

import statementledger as sl
from fastapi import (FastAPI, File, Form, HTTPException, Request,
                     UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from web import parsing
from web.store import AccountExists, DuplicateStatement, Store

MAX_MB_DEFAULT = 25


def _description(key: str) -> str:
    """First docstring line of a registry module, for humans (distilled
    modules often carry no docstring — that is fine, not a load failure)."""
    try:
        lines = (sl.load_module(key).__doc__ or "").strip().splitlines()
        return lines[0] if lines else ""
    except Exception:  # noqa: BLE001 - a broken module still gets a row
        return "(module failed to load)"


def create_app(data_root: Path | None = None,
               max_mb: float | None = None) -> FastAPI:
    app = FastAPI(title="statementledger", docs_url=None, redoc_url=None,
                  openapi_url=None)
    store = Store(Path(data_root or os.environ.get("STMTLEDGER_WEB_DATA",
                                                   "data")))
    max_bytes = int((max_mb or float(os.environ.get("STMTLEDGER_WEB_MAX_MB",
                                                    MAX_MB_DEFAULT)))
                    * 1024 * 1024)

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
        return render("account.html", request, account=account,
                      metas=store.statements(slug), ledger=_latest_ledger(slug),
                      keys=sl.keys(), outcome=None, error=None)

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
            return render("account.html", request, status=status,
                          account=account, metas=store.statements(slug),
                          ledger=_latest_ledger(slug), keys=sl.keys(),
                          outcome=None, error=message)

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
            statement = outcome["statement"]
            body = {"status": outcome["kind"], "id": statement["id"],
                    "period": statement["period"],
                    "summary": statement["summary"]}
            for extra in ("key", "attempts", "note", "suggest_pin",
                          "ambiguous"):
                if outcome.get(extra) is not None:
                    body[extra] = outcome[extra]
            return JSONResponse(body, status_code=status)
        return render("statements_region.html", request, status=status,
                      account=store.get_account(slug),
                      metas=store.statements(slug), outcome=outcome,
                      ledger=_latest_ledger(slug))

    @app.post("/accounts/{slug}/statements")
    def upload_statement(slug: str, request: Request,
                         file: UploadFile | None = File(None)):
        account = require_account(slug)

        def error(status: int, message: str):
            if wants_json(request):
                return JSONResponse({"detail": message}, status_code=status)
            return render("statements_region.html", request, status=status,
                          account=account, metas=store.statements(slug),
                          outcome={"kind": "error", "message": message},
                          ledger=_latest_ledger(slug))

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
        return render("statement.html", request, account=account, meta=meta,
                      ledger=ledger, verified=verified,
                      pdf_rel=_pdf_rel(slug, sid))

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

    app.mount("/static", StaticFiles(directory=here / "static"), name="static")
    return app


app = create_app()
