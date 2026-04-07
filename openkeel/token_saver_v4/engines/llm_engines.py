"""v4 LLM-backed engines — the features that actually make the local LLM
load-bearing in the token saver pipeline.

All engines in this module:
  - Route through summarizer.ollama_generate() which, after the 2026-04-07
    fix, resolves to qwen2.5:3b @ jagg 3090 (~200 tok/s, 0.6s per call).
  - Fail open: any exception returns None, the caller uses the rule-based
    fallback unchanged.
  - Record their own savings via the passed-in recorder fn.

Features implemented:
  * semantic_skeleton      — first-read large file → LLM skeleton
  * grep_cluster           — cluster N grep matches into M semantic groups
  * conv_block_summarize   — summarize a block of conversation turns
  * task_result_summarize  — summarize a subagent's returned output
"""

from __future__ import annotations

from typing import Optional


def _call(prompt: str, max_tokens: int = 512) -> str:
    try:
        from openkeel.token_saver.summarizer import ollama_generate
        return ollama_generate(prompt, max_tokens=max_tokens) or ""
    except Exception:
        return ""


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return t


# ---------------------------------------------------------------------------
# G. First-read semantic skeleton
# ---------------------------------------------------------------------------

_SKELETON_PROMPT = """You are reading a code file for the first time. Your job is to \
produce a compact skeleton that captures everything a developer needs to navigate the file.

Output format (plain text, no markdown fences):
  PURPOSE: one-sentence description of what this file is for
  CLASSES: comma-separated list
  FUNCTIONS: comma-separated list of top-level functions (not methods)
  KEY CONSTANTS: comma-separated list of uppercase names
  IMPORTS: comma-separated top-level module names (dedupe)
  NOTABLE: 1-3 bullets about anything non-obvious (side effects at import, threading, global state, etc.)

Be terse. No prose beyond what the format requires.

FILE: {file_path}
CONTENT:
{content}

SKELETON:"""


def semantic_skeleton(content: str, file_path: str,
                       min_chars: int = 2000) -> Optional[str]:
    """Return an LLM-generated skeleton, or None if the blob is too small/failed."""
    if len(content) < min_chars:
        return None
    # Cap content sent to the LLM — qwen2.5:3b prefers <8K context
    clipped = content if len(content) <= 6000 else (
        content[:3000] + "\n... [middle elided] ...\n" + content[-2000:]
    )
    prompt = _SKELETON_PROMPT.format(file_path=file_path, content=clipped)
    result = _call(prompt, max_tokens=400)
    if not result or len(result) < 50:
        return None
    return _strip_fences(result)


# ---------------------------------------------------------------------------
# E. Grep semantic clustering
# ---------------------------------------------------------------------------

_GREP_PROMPT = """You are given a list of grep matches. Group them into 2-5 semantic \
categories and output a compact summary.

Output format (plain text):
  [category-name] one representative line
    + N more in: file1, file2, file3
  [category-name] one representative line
    + N more in: file4, file5
  ...

Rules:
  - Categories should reflect USE (definition/import/call/test/comment/etc.) not file type.
  - Each category must have at least 1 match.
  - Use ABSOLUTE counts, not percentages.
  - Be terse. No prose outside the format.

PATTERN: {pattern}
MATCHES:
{matches}

GROUPED:"""


def grep_cluster(pattern: str, grep_output: str,
                 min_matches: int = 30) -> Optional[str]:
    """Cluster grep matches into semantic groups. Returns None on small / failed."""
    lines = [l for l in grep_output.split("\n") if l.strip()]
    if len(lines) < min_matches:
        return None
    # Cap input to ~100 representative lines to keep the LLM fast
    sample = lines[:100]
    prompt = _GREP_PROMPT.format(pattern=pattern, matches="\n".join(sample))
    result = _call(prompt, max_tokens=500)
    if not result or len(result) < 50:
        return None
    header = f"[TOKEN SAVER v4] Grep results grouped by LLM ({len(lines)} matches → {result.count(chr(10))+1} groups). Use Grep with head_limit for raw.\n\n"
    return header + _strip_fences(result)


# ---------------------------------------------------------------------------
# C. Conversation block summarizer
# ---------------------------------------------------------------------------

_CONV_PROMPT = """You are compressing an AI coding session's history. Summarize the \
following block of turns into a SHORT recap that a future agent can use.

PRESERVE (verbatim where possible):
  - Every file path mentioned
  - Every function/class/variable name mentioned
  - Every error message
  - Every decision reached ("we decided to...", "switched from X to Y")
  - Git commit hashes and branch names

DROP:
  - Narration ("I'll do this", "let me try")
  - Repeated tool outputs
  - Intermediate thought process
  - Pleasantries

Output at most 6 bullets. No prose headers.

BLOCK:
{block}

COMPRESSED RECAP:"""


def conv_block_summarize(turns_text: str,
                          min_chars: int = 800) -> Optional[str]:
    """Summarize a block of conversation turns. Returns None on small / failed."""
    if len(turns_text) < min_chars:
        return None
    clipped = turns_text[:8000] if len(turns_text) > 8000 else turns_text
    prompt = _CONV_PROMPT.format(block=clipped)
    result = _call(prompt, max_tokens=400)
    if not result or len(result) < 40:
        return None
    return _strip_fences(result)


# ---------------------------------------------------------------------------
# I. Task/Agent result summarizer
# ---------------------------------------------------------------------------

_TASK_PROMPT = """You are compressing a subagent's final report. Extract the actionable \
findings and drop the process narration.

PRESERVE:
  - File paths and line numbers
  - Function/class names
  - Concrete findings (bugs, inconsistencies, counts, measurements)
  - Any code snippets the subagent emitted

DROP:
  - "I searched for...", "I then looked at...", "I found that..."
  - Repeated framing
  - Tool call recaps

Output at most 10 bullet points. Plain text, no markdown headers.

SUBAGENT REPORT:
{report}

COMPRESSED:"""


def task_result_summarize(report_text: str,
                           min_chars: int = 1500) -> Optional[str]:
    """Summarize a subagent result. Returns None if small / failed."""
    if len(report_text) < min_chars:
        return None
    clipped = report_text[:10000] if len(report_text) > 10000 else report_text
    prompt = _TASK_PROMPT.format(report=clipped)
    result = _call(prompt, max_tokens=500)
    if not result or len(result) < 50:
        return None
    return _strip_fences(result)
