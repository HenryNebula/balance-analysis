"""Parse orchestration for the web layer: the structured unrolling of
sl.parse()'s try-every-candidate loop, so the UI gets per-parser verdicts
instead of one exception string (docs/web-ui-design.md §3.4, §7).

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


def probe(pdf: Path, candidates: list[str]) -> dict:
    """Try each candidate parser key against the PDF.

    Returns {ok, key, stmt, attempts, note, verified_keys}:
    - ok: a parser verified; stmt is the canonical dict (tagged with _key)
    - attempts: [{key, error}] for every candidate that did not verify
    - note: plain-language blocker when frames extraction itself failed
    - verified_keys: every key that verified (detection mode must know when
      the match is ambiguous — never auto-pin then, §7.2)
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            frames = sl.frames_for([Path(pdf)], Path(tmp))
    except Exception as error:  # noqa: BLE001 - surfaced as data, not a 500
        text = _error_text(error)
        if any(marker in text for marker in ENCRYPTED_MARKERS):
            note = ("The PDF is encrypted/password-protected — export an "
                    "unencrypted copy from the bank and upload that.")
        else:
            note = (f"Could not extract text from the PDF ({text[:200]}). "
                    "If it is damaged, tools/heal_pdf.py may recover it.")
        return {"ok": False, "key": None, "stmt": None, "attempts": [],
                "note": note, "verified_keys": []}

    statements = frames["statements"]
    if not statements or not statements[0].get("lines"):
        return {"ok": False, "key": None, "stmt": None, "attempts": [],
                "note": "No extractable text — is this a scanned image PDF?",
                "verified_keys": []}

    attempts: list[dict] = []
    verified_keys: list[str] = []
    stmt = None
    for key in candidates:
        try:
            module = sl.load_module(key)
        except Exception as error:  # noqa: BLE001 - a broken registry module
            attempts.append({"key": key,
                             "error": f"registry: {type(error).__name__}: "
                                      f"{error}"[:400]})
            continue
        verdict = sl.evaluate(module.parse, statements, key=key)
        if verdict["verified"]:
            verified_keys.append(key)
            if stmt is None:  # first-verified-wins, same rule as sl.parse
                stmt = module.parse(statements[0])
                stmt["_key"] = key
        else:
            attempts.extend({"key": key, "error": result["error"]}
                            for result in verdict["results"])
    return {"ok": stmt is not None, "key": stmt and stmt["_key"], "stmt": stmt,
            "attempts": attempts, "note": "", "verified_keys": verified_keys}


def candidates_for(account: dict) -> list[str]:
    """Pinned accounts parse with exactly their parser; unpinned accounts
    try every registered key (detection mode, §7.2)."""
    return [account["registry_key"]] if account.get("registry_key") else sl.keys()
