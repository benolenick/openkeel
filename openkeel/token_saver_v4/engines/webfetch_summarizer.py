"""WebFetch Summarizer — v4.2.

Summarizes a fetched web page against a concrete question, replacing
Claude's own summarization step. Direct token-for-token swap of Claude
work for jagg work.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

MIN_CHARS = 1500


@dataclass
class SummarizeDecision:
    output: str
    original_chars: int
    output_chars: int
    saved_chars: int
    latency_ms: float
    fell_back: bool = False
    reason: str = ""


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = _WS_RE.sub(" ", text)
    return text.strip()


_SYSTEM = (
    "You summarize a web page for an engineer who is investigating a "
    "specific question. Output ONLY content directly relevant to the "
    "question. Skip nav, footers, ads, repeated boilerplate. Preserve "
    "code snippets verbatim. Max 25 lines. No preamble."
)


def summarize(page_content: str, question: str = "") -> SummarizeDecision:
    text = _strip_html(page_content) if "<" in page_content[:200] else page_content
    n = len(text)
    if n < MIN_CHARS:
        return SummarizeDecision(text, n, n, 0, 0.0, True, "below_threshold")

    capped = text[:18000]
    q = question.strip() or "the main technical content"
    prompt = f"QUESTION: {q}\n\nPAGE:\n{capped}\n\nRELEVANT SUMMARY:"
    t0 = time.time()
    try:
        from openkeel.token_saver.summarizer import ollama_generate
        out = ollama_generate(prompt, system=_SYSTEM, max_tokens=500) or ""
    except Exception as e:
        return SummarizeDecision(text, n, n, 0, (time.time() - t0) * 1000,
                                 True, f"llm_error:{type(e).__name__}")
    latency_ms = (time.time() - t0) * 1000

    out = out.strip()
    if not out or len(out) >= n:
        return SummarizeDecision(text, n, n, 0, latency_ms, True, "no_compression")

    return SummarizeDecision(out, n, len(out), n - len(out), latency_ms, False, "ok")
