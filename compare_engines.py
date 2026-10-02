"""A/B the two local-LLM extraction engines on the same PDFs.

Engine A — transtractor fork: rules engine first, built-in LLM fallback
          (Rust client, balance checkers, no external deps).
Engine B — bankstatementparser: LLM path via LiteLLM, its own Golden-Rule
          verification. Its completion call has no extra_body hook, so a
          wrapper adds enable_thinking=false for Qwen3-style models.

Usage (from balance-analysis/):
    uv run python compare_engines.py [pdfs...]
    uv run python compare_engines.py path/to/statement-pdfs/
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

BASE_URL = os.environ.get("TRANSTRACTOR_LLM_URL", "http://127.0.0.1:30000/v1")
MODEL = os.environ.get("TRANSTRACTOR_LLM_MODEL", "qwen3.5-4b")
TIMEOUT = int(os.environ.get("TRANSTRACTOR_LLM_TIMEOUT", "600"))

DEFAULT_PDFS = [
    Path("/home/henryhuang/.claude/jobs/37e8a7cc/tmp/smoke_statement.pdf"),
    Path("/home/henryhuang/Documents/Code/balance-analysis/transtractor/tests/fixtures/test1.pdf"),
]
TRUTH = Path("/home/henryhuang/.claude/jobs/37e8a7cc/tmp/smoke_truth.json")


@dataclass
class EngineResult:
    engine: str
    status: str  # ok | error
    method: str = ""
    elapsed_s: float = 0.0
    txn_count: int = 0
    opening: float | None = None
    closing: float | None = None
    verification: str = ""
    amounts: list[float] | None = None
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "engine": self.engine,
            "status": self.status,
            "method": self.method,
            "elapsed_s": round(self.elapsed_s, 1),
            "txn_count": self.txn_count,
            "opening_balance": self.opening,
            "closing_balance": self.closing,
            "verification": self.verification,
            "error": self.error[:300],
        }


def run_engine_a(pdf: Path) -> EngineResult:
    """transtractor fork: rules first, built-in LLM fallback (lenient mode)."""
    from transtractor import LlmConfig, ParseError, Parser

    parser = Parser(llm=LlmConfig(base_url=BASE_URL, model=MODEL, timeout_secs=TIMEOUT, no_think=True))
    started = time.perf_counter()
    try:
        sd = parser.parse(str(pdf), lenient=True)
    except ParseError as error:
        return EngineResult("A transtractor", "error", elapsed_s=time.perf_counter() - started, error=str(error))
    elapsed = time.perf_counter() - started
    if sd.errors:
        verification = f"DISCREPANCY ({len(sd.errors)} checker errors)"
    else:
        verification = "VERIFIED (balance checkers)"
    return EngineResult(
        "A transtractor",
        "ok",
        method=sd.key or "",
        elapsed_s=elapsed,
        txn_count=len(sd.transactions),
        opening=sd.opening_balance,
        closing=sd.closing_balance,
        verification=verification,
        amounts=[round(t.amount, 2) for t in sd.transactions],
        error=" | ".join(sd.errors[:2]),
    )


def run_engine_b(pdf: Path) -> EngineResult:
    """bankstatementparser smart_ingest via LiteLLM at the same endpoint."""
    import litellm
    from bankstatementparser.hybrid.llm_extractor import LLMExtractor
    from bankstatementparser.hybrid.orchestrator import smart_ingest

    def completion_no_think(**kwargs):
        # BSP's extractor has no extra_body hook; inject it here so Qwen3
        # thinking models answer instead of burning tokens reasoning.
        kwargs.setdefault("api_key", "sk-local")
        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        kwargs.setdefault("timeout", TIMEOUT)
        return litellm.completion(**kwargs)

    extractor = LLMExtractor(
        model=f"openai/{MODEL}",
        api_base=BASE_URL,
        completion_fn=completion_no_think,
    )
    started = time.perf_counter()
    try:
        result = smart_ingest(pdf, extractor=extractor)
    except Exception as error:  # noqa: BLE001 - report any engine failure
        return EngineResult(
            "B bankstatementparser", "error", elapsed_s=time.perf_counter() - started, error=f"{type(error).__name__}: {error}"
        )
    elapsed = time.perf_counter() - started

    verification = ""
    if result.verification is not None:
        verification = str(getattr(result.verification, "status", "") or "")
    opening = getattr(result.verification, "opening_balance", None) if result.verification else None
    closing = getattr(result.verification, "closing_balance", None) if result.verification else None
    amounts = []
    for txn in result.transactions:
        amount = getattr(txn, "amount", None)
        amounts.append(round(float(amount), 2) if amount is not None else 0.0)
    return EngineResult(
        "B bankstatementparser",
        "ok",
        method=result.source_method,
        elapsed_s=elapsed,
        txn_count=len(result.transactions),
        opening=float(opening) if opening is not None else None,
        closing=float(closing) if closing is not None else None,
        verification=verification,
        amounts=amounts,
    )


def compare_against_truth(result: EngineResult, truth: dict) -> str:
    if result.status != "ok":
        return "n/a"
    expected = [round(t["amount"], 2) for t in truth["transactions"]]
    if result.amounts != expected:
        first_bad = next(
            (i + 1 for i, (a, e) in enumerate(zip(result.amounts, expected, strict=False)) if a != e),
            "len",
        )
        return f"DIFFERS (first mismatch at row {first_bad})"
    ok_balances = (
        result.opening is not None
        and abs(result.opening - truth["opening_balance"]) <= 0.01
        and abs(result.closing - truth["closing_balance"]) <= 0.01
    )
    return "exact" if ok_balances else "amounts exact, balances differ"


def cross_engine(a: EngineResult, b: EngineResult) -> str:
    if a.status != "ok" or b.status != "ok":
        return "n/a (one engine failed)"
    if a.amounts == b.amounts:
        return "identical extractions"
    first_bad = next(
        (i + 1 for i, (x, y) in enumerate(zip(a.amounts, b.amounts, strict=False)) if x != y),
        "len",
    )
    return f"DISAGREE (first mismatch at row {first_bad})"


def main() -> int:
    args = sys.argv[1:]
    if not args:
        pdfs = DEFAULT_PDFS
    else:
        target = Path(args[0])
        pdfs = sorted(target.rglob("*.pdf")) if target.is_dir() else [Path(p) for p in args]

    truth = json.loads(TRUTH.read_text()) if TRUTH.exists() else None
    print(f"endpoint {BASE_URL}  model {MODEL}\n")
    report = []

    for pdf in pdfs:
        print(f"=== {pdf.name} ===")
        a = run_engine_a(pdf)
        b = run_engine_b(pdf)
        for result in (a, b):
            print(f"  {result.to_dict()}")
        if truth and pdf.name == "smoke_statement.pdf":
            print(f"  A vs truth : {compare_against_truth(a, truth)}")
            print(f"  B vs truth : {compare_against_truth(b, truth)}")
        print(f"  A vs B     : {cross_engine(a, b)}")
        print()
        report.append({"pdf": str(pdf), "A": a.to_dict(), "B": b.to_dict()})

    out = Path("ab_report.json")
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
