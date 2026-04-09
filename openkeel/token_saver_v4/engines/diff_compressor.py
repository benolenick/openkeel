"""Diff Compressor — v4.2.

Compresses git diff/log/show output into semantic change lists. Pure
input filter: Claude reads a denser version of the same information.

Safe failure mode: on any LLM error or short input, returns the original
blob untouched.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

MIN_CHARS = 800  # below this, regex/raw is fine


@dataclass
class CompressDecision:
    output: str
    original_chars: int
    output_chars: int
    saved_chars: int
    latency_ms: float
    fell_back: bool = False
    reason: str = ""


_SYSTEM = (
    "You compress git diffs into a tight semantic change list for an "
    "engineer. Drop pure whitespace/import-shuffle/formatting churn. "
    "Keep behavior changes, signature changes, new functions, deleted "
    "code, and any line touching error handling or control flow. Output "
    "as bullet points: '- file:lineno  what changed'. No prose."
)


def compress(diff_text: str) -> CompressDecision:
    n = len(diff_text)
    if n < MIN_CHARS:
        return CompressDecision(diff_text, n, n, 0, 0.0, True, "below_threshold")

    # Cap input — very large diffs get truncated to keep latency bounded.
    capped = diff_text[:20000]

    prompt = f"DIFF:\n{capped}\n\nCOMPRESSED CHANGE LIST:"
    t0 = time.time()
    try:
        from openkeel.token_saver.summarizer import ollama_generate
        out = ollama_generate(prompt, system=_SYSTEM, max_tokens=500) or ""
    except Exception as e:
        return CompressDecision(diff_text, n, n, 0, (time.time() - t0) * 1000,
                                True, f"llm_error:{type(e).__name__}")
    latency_ms = (time.time() - t0) * 1000

    out = out.strip()
    if not out or len(out) >= n:
        return CompressDecision(diff_text, n, n, 0, latency_ms, True, "no_compression")

    return CompressDecision(out, n, len(out), n - len(out), latency_ms, False, "ok")
