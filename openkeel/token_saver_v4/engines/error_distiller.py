"""Error Distiller — v4.2.

Takes a stack trace, test failure, or error log and returns the actual
error + the user-code frames, dropping framework noise.

This is qwen's sweet spot: language-aware filtering that regex can't do
because the noise patterns differ per language/framework.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

MIN_CHARS = 600


@dataclass
class DistillDecision:
    output: str
    original_chars: int
    output_chars: int
    saved_chars: int
    latency_ms: float
    fell_back: bool = False
    reason: str = ""


_SYSTEM = (
    "You distill error output for an engineer. Given a stack trace, "
    "test failure, or log dump, output ONLY:\n"
    "  1. The exception type and message\n"
    "  2. The 3 most relevant frames in user code (skip framework/stdlib)\n"
    "  3. Any obvious cause hint (one line)\n"
    "Format as plain text, max 15 lines. No preamble."
)


def distill(error_text: str) -> DistillDecision:
    n = len(error_text)
    if n < MIN_CHARS:
        return DistillDecision(error_text, n, n, 0, 0.0, True, "below_threshold")

    capped = error_text[:15000]
    prompt = f"ERROR OUTPUT:\n{capped}\n\nDISTILLED:"
    t0 = time.time()
    try:
        from openkeel.token_saver.summarizer import ollama_generate
        out = ollama_generate(prompt, system=_SYSTEM, max_tokens=300) or ""
    except Exception as e:
        return DistillDecision(error_text, n, n, 0, (time.time() - t0) * 1000,
                               True, f"llm_error:{type(e).__name__}")
    latency_ms = (time.time() - t0) * 1000

    out = out.strip()
    if not out or len(out) >= n:
        return DistillDecision(error_text, n, n, 0, latency_ms, True, "no_compression")

    return DistillDecision(out, n, len(out), n - len(out), latency_ms, False, "ok")
