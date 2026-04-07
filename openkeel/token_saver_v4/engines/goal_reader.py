"""Goal-Conditioned File Reader — v4.3.

The single biggest leverage point in the token saver: filter file content
by the agent's current goal before it ever reaches Claude.

Today, large file reads go through:
  1. cache_hit (re-reads only) — generic summary
  2. v4_semantic_skeleton — generic structural summary
  3. large_file_compress — head + structure + tail

None of those know WHY Claude is reading the file. This engine does.

Pure input filter:
  - Input: file content + optional goal string
  - Output: only the lines relevant to the goal, with line numbers preserved
  - Worst case: empty goal → falls back to general-purpose filter
  - Worst-worst case: LLM error → returns original content unchanged

Goal source order (first hit wins):
  1. TOKEN_SAVER_GOAL env var
  2. ~/.openkeel/current_goal.txt
  3. Last entry from ~/.openkeel/distilled_log.jsonl (if it exists)
  4. Empty string (engine runs in goal-agnostic mode)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

MIN_CHARS = 5000  # below this, generic skeleton is fine
MAX_INPUT_CHARS = 30000  # cap to keep latency under ~3s


@dataclass
class GoalReadDecision:
    output: str
    original_chars: int
    output_chars: int
    saved_chars: int
    latency_ms: float
    goal_used: str
    fell_back: bool = False
    reason: str = ""


def get_current_goal() -> str:
    """Resolve the agent's current goal from the cheapest available source."""
    env_goal = os.environ.get("TOKEN_SAVER_GOAL", "").strip()
    if env_goal:
        return env_goal[:300]

    goal_file = Path.home() / ".openkeel" / "current_goal.txt"
    if goal_file.exists():
        try:
            text = goal_file.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text[:300]
        except Exception:
            pass

    log_file = Path.home() / ".openkeel" / "distilled_log.jsonl"
    if log_file.exists():
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for line in reversed(lines[-20:]):
                try:
                    entry = json.loads(line)
                    for key in ("goal", "hypothesis", "intent", "task"):
                        v = entry.get(key)
                        if isinstance(v, str) and v.strip():
                            return v.strip()[:300]
                except Exception:
                    continue
        except Exception:
            pass

    return ""


def _build_prompt(content: str, goal: str, file_path: str) -> tuple[str, str]:
    if goal:
        system = (
            "You are a precision filter for source code. Given a file and "
            "an engineer's current goal, return ONLY the parts of the file "
            "directly relevant to that goal. Preserve line numbers exactly "
            "as shown. Drop unrelated functions, classes, comments, and "
            "boilerplate. If a function partially matches, include the whole "
            "function. Format: each kept line as 'LNNN: <code>'. Group "
            "consecutive kept lines together. Use '... (N lines omitted) ...' "
            "between groups. Be conservative — when unsure, KEEP."
        )
        prompt = (
            f"GOAL: {goal}\n\nFILE: {file_path}\n\n"
            f"CONTENT (with line numbers):\n{content}\n\n"
            f"FILTERED RELEVANT LINES:"
        )
    else:
        system = (
            "You are a precision filter for source code. Return only the "
            "parts of this file most useful for a software engineer reading "
            "it for the first time: public API, key classes/functions, "
            "non-obvious logic. Drop comments, docstrings, imports beyond "
            "the first 5, and trivial helpers. Preserve line numbers as "
            "'LNNN: <code>'. Use '... (N lines omitted) ...' between groups."
        )
        prompt = (
            f"FILE: {file_path}\n\nCONTENT (with line numbers):\n{content}"
            f"\n\nFILTERED:"
        )
    return system, prompt


def _number_lines(content: str) -> str:
    """Add line numbers to content for the LLM to reference."""
    lines = content.split("\n")
    return "\n".join(f"L{i+1:04d}: {line}" for i, line in enumerate(lines))


def filter_by_goal(content: str, goal: str = "", file_path: str = "") -> GoalReadDecision:
    """Run the goal-conditioned filter. Always safe to call."""
    n = len(content)
    if n < MIN_CHARS:
        return GoalReadDecision(
            content, n, n, 0, 0.0, goal, True, "below_threshold",
        )

    # v5 BYPASS: never run a text LLM filter on Python / code files.
    # Observed in session 2026-04-07: this filter stripped HEALTH_CHECKS
    # dict entries and a subprocess.run() call from monitor_cron.py, making
    # the file unreadable until I used offset/limit to get the raw source.
    # Code surgery needs exact content; the LLM-as-filter approach is
    # structurally wrong for source files. Prose files (md, txt, logs) are
    # still fair game.
    _code_suffixes = (
        ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
        ".c", ".cc", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift",
        ".kt", ".scala", ".sh", ".bash", ".zsh", ".yaml", ".yml",
        ".toml", ".json", ".xml", ".html", ".css", ".sql",
    )
    if file_path and file_path.lower().endswith(_code_suffixes):
        return GoalReadDecision(
            content, n, n, 0, 0.0, goal, True, "code_file_bypass",
        )

    # Cap input — very large files get truncated. We bias toward the head
    # since most tasks care about the public API/imports.
    if n > MAX_INPUT_CHARS:
        capped = content[:MAX_INPUT_CHARS]
        truncation_note = True
    else:
        capped = content
        truncation_note = False

    numbered = _number_lines(capped)
    system, prompt = _build_prompt(numbered, goal, file_path)

    t0 = time.time()
    try:
        from openkeel.token_saver.summarizer import ollama_generate
        out = ollama_generate(prompt, system=system, max_tokens=900) or ""
    except Exception as e:
        return GoalReadDecision(
            content, n, n, 0, (time.time() - t0) * 1000, goal,
            True, f"llm_error:{type(e).__name__}",
        )
    latency_ms = (time.time() - t0) * 1000

    out = out.strip()
    if not out or len(out) >= n * 0.9:
        return GoalReadDecision(
            content, n, n, 0, latency_ms, goal,
            True, "no_meaningful_compression",
        )

    if truncation_note:
        out = (f"[NOTE: file was {n} chars; only the first {MAX_INPUT_CHARS} "
               f"were filtered. Use Read with offset for the rest.]\n\n{out}")

    return GoalReadDecision(
        output=out,
        original_chars=n,
        output_chars=len(out),
        saved_chars=n - len(out),
        latency_ms=latency_ms,
        goal_used=goal or "(none — generic filter)",
        fell_back=False,
        reason="ok",
    )
