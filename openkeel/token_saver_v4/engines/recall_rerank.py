"""Recall Rerank — v4.1 experiment.

Hypothesis (from the v4 honest review): the local LLM on jagg is paying
its rent on bash_llm_summarize but losing money everywhere else, because
it is being used as a *replacement for Claude's output* (LocalEdit) when
it should be a *filter in front of Claude's input*.

This engine is the first concrete test of "LLM as input filter":
  - Takes a Hyphae /recall response (a list of result dicts)
  - Asks qwen2.5:3b on jagg "which of these actually answer the query?"
  - Returns only the kept indices

Pure read-side. No writes to user files. If qwen2.5:3b drops the wrong
result, the worst case is Claude has slightly less context — same as a
top_k that was set too low. There is zero correctness risk on user code.

Usage:
    from openkeel.token_saver_v4.engines import recall_rerank
    decision = recall_rerank.rerank(query, results)
    kept = [results[i] for i in decision.kept_indices]
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any


# Don't bother below this many results — top_k 1-3 is already cheap.
MIN_RESULTS = 4
# Don't bother below this many chars total — small payloads cost more
# in round-trip than they save.
MIN_TOTAL_CHARS = 600
# Hard cap on how much text we ship to the local LLM. Larger payloads
# get truncated per-result; we never want to spend >1 second on rerank.
MAX_RESULT_CHARS = 800


@dataclass
class RerankDecision:
    kept_indices: list[int]
    original_chars: int
    kept_chars: int
    saved_chars: int
    latency_ms: float
    fell_back: bool = False
    reason: str = ""

    @property
    def ratio(self) -> float:
        if not self.original_chars:
            return 0.0
        return self.saved_chars / self.original_chars


def _result_text(r: Any) -> str:
    """Extract the text body from a Hyphae result dict (or any dict-ish)."""
    if isinstance(r, str):
        return r
    if isinstance(r, dict):
        for k in ("text", "content", "body", "summary", "snippet"):
            v = r.get(k)
            if isinstance(v, str) and v:
                return v
        # Last resort: serialize
        return json.dumps(r, default=str)
    return str(r)


def _measure(results: list) -> int:
    return sum(len(_result_text(r)) for r in results)


def _build_prompt(query: str, results: list) -> str:
    lines = [
        "You are a precision filter for a memory recall system.",
        f"QUERY: {query}",
        "",
        "RESULTS (each numbered):",
    ]
    for i, r in enumerate(results):
        body = _result_text(r)[:MAX_RESULT_CHARS].replace("\n", " ").strip()
        lines.append(f"[{i}] {body}")
    lines += [
        "",
        "Return ONLY a JSON array of integer indices for results that "
        "directly answer the query. Drop generic/off-topic results. "
        "If unsure, KEEP the result. If all are relevant, return all "
        "indices. Example output: [0, 2, 3]",
        "JSON:",
    ]
    return "\n".join(lines)


_INDEX_PATTERN = re.compile(r"\[\s*(?:\d+\s*,\s*)*\d*\s*\]")


def _parse_indices(raw: str, n: int) -> list[int] | None:
    """Pull the first JSON-array-of-ints out of an LLM response."""
    if not raw:
        return None
    m = _INDEX_PATTERN.search(raw)
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(arr, list):
        return None
    out: list[int] = []
    for x in arr:
        if isinstance(x, int) and 0 <= x < n and x not in out:
            out.append(x)
    return out


def rerank(query: str, results: list) -> RerankDecision:
    """Run the rerank decision. Always safe to call — falls back to
    keep-all on any error or low-value input."""
    n = len(results)
    original = _measure(results)

    if n < MIN_RESULTS or original < MIN_TOTAL_CHARS:
        return RerankDecision(
            kept_indices=list(range(n)),
            original_chars=original,
            kept_chars=original,
            saved_chars=0,
            latency_ms=0.0,
            fell_back=True,
            reason="below_threshold",
        )

    prompt = _build_prompt(query, results)

    t0 = time.time()
    try:
        from openkeel.token_saver.summarizer import ollama_generate
        raw = ollama_generate(prompt, system="", max_tokens=80) or ""
    except Exception as e:
        return RerankDecision(
            kept_indices=list(range(n)),
            original_chars=original,
            kept_chars=original,
            saved_chars=0,
            latency_ms=(time.time() - t0) * 1000,
            fell_back=True,
            reason=f"llm_error:{type(e).__name__}",
        )
    latency_ms = (time.time() - t0) * 1000

    kept = _parse_indices(raw, n)
    if kept is None or not kept:
        # Empty or unparseable -> keep all (safe).
        return RerankDecision(
            kept_indices=list(range(n)),
            original_chars=original,
            kept_chars=original,
            saved_chars=0,
            latency_ms=latency_ms,
            fell_back=True,
            reason="parse_failed_or_empty",
        )

    kept_chars = sum(len(_result_text(results[i])) for i in kept)
    return RerankDecision(
        kept_indices=kept,
        original_chars=original,
        kept_chars=kept_chars,
        saved_chars=original - kept_chars,
        latency_ms=latency_ms,
        fell_back=False,
        reason="ok",
    )
