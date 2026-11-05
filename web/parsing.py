"""Parse orchestration for the web layer: the structured unrolling of
sl.parse()'s try-every-candidate loop, so the UI gets per-parser verdicts
instead of one exception string (docs/web-ui-design.md §4.4, §7).

One frames extraction per PDF (pdfplumber is the expensive step); every
candidate parser is then evaluated against those frames. Outcomes carry
`attempts` (per-key {key, error}) for the failure UI and plain-language
notes for the shapes that never reach a parser (encrypted, scanned).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import statementledger as sl

# Shapes that mean "this PDF cannot be frame-extracted at all" — surfaced as
# plain language, never as a pdfplumber traceback (§10).
ENCRYPTED_MARKERS = ("password", "encrypted", "PDFPasswordIncorrect")


def _error_text(error: BaseException) -> str:
    """Exception text including wrapped causes — pdfplumber re-raises pdfminer
    errors as bare PdfminerException(args) with an empty str()."""
    parts = [f"{type(error).__name__}: {error}"]
    parts.extend(repr(arg) for arg in getattr(error, "args", ()) if arg is not error)
    if error.__cause__:
        parts.append(_error_text(error.__cause__))
    return " ".join(parts)


def _frames_or_note(pdf: Path) -> tuple[list[dict], str]:
    """Extract frames for one PDF; on failure return the plain-language
    blocker instead of a pdfplumber traceback (§10). Broken-glyph PDFs are
    healed inside frames_for() — only the unrecoverable shapes reach here."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            frames = sl.frames_for([Path(pdf)], Path(tmp))
    except sl.ScannedPdfError as error:
        return [], (f"{error}. Statementledger reads printed text only — "
                    "OCR the scan and upload the OCR'd copy.")
    except Exception as error:  # noqa: BLE001 - surfaced as data, not a 500
        text = _error_text(error)
        if any(marker in text for marker in ENCRYPTED_MARKERS):
            return [], ("The PDF is encrypted/password-protected — export an "
                        "unencrypted copy from the bank and upload that.")
        return [], (f"Could not extract text from the PDF ({text[:200]}). "
                    "If it is damaged, tools/heal_pdf.py may recover it.")
    statements = frames["statements"]
    if not statements or not statements[0].get("lines"):
        return [], ("No extractable text and no images either — the PDF "
                    "looks empty or damaged.")
    return statements, ""


def _evaluate_all(statements: list[dict], candidates: list[str],
                   overlay: Path | None = None):
    """Every candidate's verdict over the (already extracted) frames:
    per-key results and the list of verified keys. `overlay` pins the user
    registry (the env default is process-global; each app instance owns
    its own)."""
    results: list[dict] = []
    verified: list[str] = []
    for key in candidates:
        try:
            module = sl.load_module(key, overlay)
        except Exception as error:  # noqa: BLE001 - a broken registry module
            results.append({"key": key, "ok": False, "rows": 0,
                            "error": f"registry: {type(error).__name__}: "
                                     f"{error}"[:400]})
            continue
        verdict = sl.evaluate(module.parse, statements, key=key)
        ok = verdict["verified"]
        results.append({"key": key, "ok": ok,
                        "rows": verdict["results"][0]["rows"],
                        "error": "" if ok else
                        verdict["results"][0]["error"]})
        if ok:
            verified.append(key)
    return results, verified


def probe(pdf: Path, candidates: list[str],
          overlay: Path | None = None) -> dict:
    """Try each candidate parser key against the PDF.

    Returns {ok, key, stmt, attempts, note, verified_keys}:
    - ok: a parser verified; stmt is the canonical dict (tagged with _key)
    - attempts: [{key, error}] for every candidate that did not verify
    - note: plain-language blocker when frames extraction itself failed
    - verified_keys: every key that verified (detection mode must know when
      the match is ambiguous — never auto-pin then, §7.2)
    """
    statements, note = _frames_or_note(pdf)
    if not statements:
        return {"ok": False, "key": None, "stmt": None, "attempts": [],
                "note": note, "verified_keys": []}

    results, verified_keys = _evaluate_all(statements, candidates, overlay)
    attempts = [{"key": r["key"], "error": r["error"]}
                for r in results if not r["ok"]]
    stmt = None
    if verified_keys:  # first-verified-wins, same rule as sl.parse
        module = sl.load_module(verified_keys[0], overlay)
        stmt = module.parse(statements[0])
        stmt["_key"] = verified_keys[0]
    return {"ok": stmt is not None, "key": stmt and stmt["_key"], "stmt": stmt,
            "attempts": attempts, "note": "", "verified_keys": verified_keys}


def probe_all(pdf: Path, overlay: Path | None = None) -> dict:
    """Dry-run try-all (§7.3): per-key {key, ok, rows, error} results and the
    verified keys, with nothing parsed-and-kept or persisted."""
    statements, note = _frames_or_note(pdf)
    if not statements:
        return {"results": [], "verified_keys": [], "note": note}
    results, verified = _evaluate_all(statements, sl.keys(overlay), overlay)
    return {"results": results, "verified_keys": verified, "note": ""}


def candidates_for(account: dict, overlay: Path | None = None) -> list[str]:
    """Pinned accounts parse with exactly their parser; unpinned accounts
    try every registered key (detection mode, §7.2)."""
    if account.get("registry_key"):
        return [account["registry_key"]]
    return sl.keys(overlay)
