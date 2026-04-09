"""Subagent Filter — v4.4.

The closest workable approximation to filtering subagent return values,
given Claude Code's hook architecture (PostToolUse cannot rewrite a
delivered tool result).

Strategy: filter the PROMPT going IN to the subagent, and inject a
concision directive that constrains the RESPONSE coming OUT. Both sides
of the agent boundary get smaller. Local LLM does real compressive work
on the prompt; the directive does the work on the response side.

Engine signature:
    decision = subagent_filter.compress_prompt(prompt, description)
    if not decision.fell_back:
        rewritten = decision.output  # use this as the new agent prompt

Pure read-side. The filtered prompt is suggested via a PreToolUse block
reason — Claude re-spawns the agent with the tighter version. No writes
to user files, no persistence beyond the ledger.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# Below this size the round-trip isn't worth it.
MIN_PROMPT_CHARS = 2500
# Hard cap on input we ship to qwen. Larger prompts get truncated.
MAX_PROMPT_CHARS = 25000

CONCISION_DIRECTIVE = (
    "\n\n---\n"
    "When done, end your response with a section labeled exactly "
    "'## ANSWER:' followed by no more than 25 lines that directly "
    "answer the request. Skip preamble, exploration narrative, and "
    "interim findings. Preserve any file paths and code snippets exactly."
)


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
    "You compress an agent task prompt for an engineer. The prompt may "
    "include pasted file content, prior conversation context, and "
    "boilerplate framing. Return a TIGHTER version that preserves: the "
    "core task, all file paths, all code snippets, all explicit "
    "constraints. Drop: redundant context dumps, polite framing, "
    "duplicate information, and any 'background' that does not affect "
    "the task. Keep file paths and code blocks verbatim. Output only "
    "the rewritten prompt — no preamble, no explanation."
)


def compress_prompt(prompt: str, description: str = "") -> CompressDecision:
    """Compress an Agent tool prompt. Returns the original on any error or
    when compression isn't worth it."""
    n = len(prompt)
    if n < MIN_PROMPT_CHARS:
        return CompressDecision(prompt, n, n, 0, 0.0, True, "below_threshold")

    capped = prompt[:MAX_PROMPT_CHARS]
    truncated = n > MAX_PROMPT_CHARS

    user_msg = (
        f"TASK DESCRIPTION: {description}\n\n"
        f"ORIGINAL PROMPT:\n{capped}\n\n"
        f"COMPRESSED PROMPT:"
    )

    t0 = time.time()
    try:
        from openkeel.token_saver.summarizer import ollama_generate
        out = ollama_generate(user_msg, system=_SYSTEM, max_tokens=600) or ""
    except Exception as e:
        return CompressDecision(
            prompt, n, n, 0, (time.time() - t0) * 1000,
            True, f"llm_error:{type(e).__name__}",
        )
    latency_ms = (time.time() - t0) * 1000

    out = out.strip()
    # Reject if the LLM returned something useless or larger than original
    if not out or len(out) >= n * 0.85:
        return CompressDecision(
            prompt, n, n, 0, latency_ms, True, "no_meaningful_compression",
        )

    # Append the concision directive — this is the half that controls the
    # response side. The subagent sees it as part of its instructions.
    final = out + CONCISION_DIRECTIVE
    if truncated:
        final = (
            "[NOTE: original prompt was truncated for compression. The "
            "essential content is preserved.]\n\n" + final
        )

    saved = n - len(final)
    if saved <= 0:
        return CompressDecision(prompt, n, n, 0, latency_ms, True, "directive_overhead")

    return CompressDecision(
        output=final,
        original_chars=n,
        output_chars=len(final),
        saved_chars=saved,
        latency_ms=latency_ms,
        fell_back=False,
        reason="ok",
    )
