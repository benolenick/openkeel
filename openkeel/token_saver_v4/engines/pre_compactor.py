"""Pre-Compactor — v4.2.

The highest-leverage v4.2 idea: when Claude Code's conversation gets
large enough that auto-compaction is approaching, run a local LLM as
the FIRST pass over the transcript to drop:

  - Tool results that were never referenced again after their turn
  - Intermediate "let me check X" thoughts that didn't lead anywhere
  - Duplicate file reads (when the same file appears 2+ times, keep
    only the latest one — older content is stale)
  - Long bash outputs whose only useful information is "exit code 0"

What remains is what Claude's own compaction step then has to compress,
which is much cheaper because the bytes are already pruned.

Input shape: a list of {role, kind, content, ref_id} entries.
Output: same list, with low-value entries replaced by "[pruned: <reason>]".

This engine is designed to be run *offline* on a transcript dump. Wiring
it into the live compaction path is a separate (riskier) project.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

MIN_TRANSCRIPT_CHARS = 4000


@dataclass
class CompactDecision:
    kept: list[dict]
    original_chars: int
    output_chars: int
    saved_chars: int
    latency_ms: float
    fell_back: bool = False
    reason: str = ""
    pruned_count: int = 0


def _measure(entries: list[dict]) -> int:
    return sum(len(str(e.get("content", ""))) for e in entries)


def _deterministic_pass(entries: list[dict]) -> tuple[list[dict], int]:
    """Cheap deterministic pruning before invoking the LLM:
      - Drop duplicate file_read of the same path (keep latest)
      - Replace bash_output with 'exit 0 / no useful output' if content
        is just whitespace, prompt noise, or a single-line OK.
    Returns (pruned_entries, count_pruned)."""
    pruned = 0
    seen_reads: dict[str, int] = {}  # path -> last index
    # First pass: find latest read per path
    for i, e in enumerate(entries):
        if e.get("kind") == "file_read":
            path = e.get("ref_id") or e.get("path") or ""
            if path:
                seen_reads[path] = i

    out: list[dict] = []
    for i, e in enumerate(entries):
        kind = e.get("kind", "")
        content = str(e.get("content", ""))

        if kind == "file_read":
            path = e.get("ref_id") or e.get("path") or ""
            if path and seen_reads.get(path) != i:
                out.append({**e, "content": f"[pruned: stale read of {path}]"})
                pruned += 1
                continue

        if kind == "bash_output":
            stripped = content.strip()
            if not stripped or stripped in {"0", "OK", "ok", "done"} or len(stripped) < 20:
                out.append({**e, "content": "[pruned: bash exit, no output]"})
                pruned += 1
                continue

        out.append(e)
    return out, pruned


def _llm_pass(entries: list[dict]) -> tuple[list[dict], int, float]:
    """Ask qwen which entries can be safely dropped. Returns
    (pruned_entries, count_pruned, latency_ms)."""
    # Build a numbered manifest the model can reference by index
    lines = []
    for i, e in enumerate(entries):
        kind = e.get("kind", "?")
        body = str(e.get("content", ""))[:200].replace("\n", " ")
        lines.append(f"[{i}] {kind}: {body}")
    manifest = "\n".join(lines)

    system = (
        "You are pruning a Claude Code conversation transcript before "
        "compaction. Return a JSON array of entry indices that are "
        "SAFE TO DROP because they are: stale duplicate reads, "
        "intermediate dead-end exploration, or low-value tool noise. "
        "Be CONSERVATIVE — when unsure, KEEP. Never drop edits, writes, "
        "errors, or the latest read of any file."
    )
    prompt = f"TRANSCRIPT MANIFEST:\n{manifest}\n\nINDICES TO DROP (JSON array):"

    t0 = time.time()
    try:
        from openkeel.token_saver.summarizer import ollama_generate
        raw = ollama_generate(prompt, system=system, max_tokens=120) or ""
    except Exception:
        return entries, 0, (time.time() - t0) * 1000
    latency_ms = (time.time() - t0) * 1000

    import re, json
    m = re.search(r"\[\s*(?:\d+\s*,\s*)*\d*\s*\]", raw)
    if not m:
        return entries, 0, latency_ms
    try:
        drop = set(int(x) for x in json.loads(m.group(0))
                   if isinstance(x, int) and 0 <= x < len(entries))
    except Exception:
        return entries, 0, latency_ms

    out = []
    for i, e in enumerate(entries):
        if i in drop:
            out.append({**e, "content": f"[pruned by llm: {e.get('kind','?')}]"})
        else:
            out.append(e)
    return out, len(drop), latency_ms


def compact(entries: list[dict]) -> CompactDecision:
    n = _measure(entries)
    if n < MIN_TRANSCRIPT_CHARS or len(entries) < 6:
        return CompactDecision(entries, n, n, 0, 0.0, True, "below_threshold")

    # Deterministic first
    after_det, det_pruned = _deterministic_pass(entries)
    # LLM second
    after_llm, llm_pruned, latency_ms = _llm_pass(after_det)

    out_chars = _measure(after_llm)
    return CompactDecision(
        kept=after_llm,
        original_chars=n,
        output_chars=out_chars,
        saved_chars=n - out_chars,
        latency_ms=latency_ms,
        fell_back=False,
        reason="ok",
        pruned_count=det_pruned + llm_pruned,
    )
