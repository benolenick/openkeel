"""Task Router — classifies tasks and routes simple ones to local model.

When Claude receives a task, the pre-hook classifies it:
  - trivial: rename, add import, simple format → local model handles it
  - moderate: code review, summarize, explain → local model with verification
  - complex: architecture, debugging, multi-file → pass to Claude

The local model result is returned as a "suggestion" that Claude can
accept or override, saving tokens on the generation side.

All routing decisions are logged to the ledger.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from openkeel.token_saver import summarizer, ledger

# Task patterns that can be handled locally
_TRIVIAL_PATTERNS = [
    (re.compile(r"add\s+import", re.I), "add_import"),
    (re.compile(r"rename\s+\w+\s+to", re.I), "rename"),
    (re.compile(r"remove\s+(unused|dead)\s+(import|variable|function)", re.I), "remove_unused"),
    (re.compile(r"fix\s+(typo|spelling|whitespace)", re.I), "fix_typo"),
    (re.compile(r"add\s+type\s+(hint|annotation)", re.I), "add_types"),
    (re.compile(r"format\s+(code|file)", re.I), "format"),
    (re.compile(r"sort\s+imports", re.I), "sort_imports"),
]

_MODERATE_PATTERNS = [
    (re.compile(r"summarize|explain|describe", re.I), "summarize"),
    (re.compile(r"review\s+(this|the)?\s*(diff|changes|code|pr)", re.I), "review"),
    (re.compile(r"what\s+(does|is)\s+\w+", re.I), "explain"),
    (re.compile(r"list\s+(all|the)\s+\w+", re.I), "list"),
    (re.compile(r"find\s+(all|the|where)", re.I), "find"),
    (re.compile(r"classify|categorize", re.I), "classify"),
    (re.compile(r"extract\s+(from|the)", re.I), "extract"),
]


def classify_task(description: str) -> dict[str, Any]:
    """Classify a task's difficulty and whether local model can handle it.

    Returns:
        {
            "difficulty": "trivial" | "moderate" | "complex",
            "task_type": str,
            "local_capable": bool,
            "confidence": float,
            "pattern_match": str | None,
        }
    """
    desc_lower = description.lower().strip()

    # Check trivial patterns first
    for pattern, task_type in _TRIVIAL_PATTERNS:
        if pattern.search(desc_lower):
            result = {
                "difficulty": "trivial",
                "task_type": task_type,
                "local_capable": True,
                "confidence": 0.9,
                "pattern_match": task_type,
            }
            _log_classification(description, result)
            return result

    # Check moderate patterns
    for pattern, task_type in _MODERATE_PATTERNS:
        if pattern.search(desc_lower):
            result = {
                "difficulty": "moderate",
                "task_type": task_type,
                "local_capable": True,
                "confidence": 0.7,
                "pattern_match": task_type,
            }
            _log_classification(description, result)
            return result

    # Heuristic signals for complexity
    complexity_signals = 0
    if len(description) > 500:
        complexity_signals += 1
    if any(w in desc_lower for w in ("refactor", "architect", "design", "debug", "investigate")):
        complexity_signals += 2
    if any(w in desc_lower for w in ("multiple files", "across", "codebase", "all files")):
        complexity_signals += 1
    if any(w in desc_lower for w in ("why", "how should", "what's the best", "trade-off")):
        complexity_signals += 2

    if complexity_signals >= 2:
        result = {
            "difficulty": "complex",
            "task_type": "complex",
            "local_capable": False,
            "confidence": 0.6 + (complexity_signals * 0.1),
            "pattern_match": None,
        }
    else:
        # Default: moderate, let local model try
        result = {
            "difficulty": "moderate",
            "task_type": "general",
            "local_capable": True,
            "confidence": 0.5,
            "pattern_match": None,
        }

    _log_classification(description, result)
    return result


def attempt_local(
    description: str,
    file_content: str = "",
    file_path: str = "",
) -> dict[str, Any]:
    """Attempt to handle a task locally. Returns result or failure.

    Returns:
        {
            "success": bool,
            "result": str,  # the generated content/answer
            "confidence": float,
            "tokens_saved_est": int,
        }
    """
    if not summarizer.is_available():
        return {"success": False, "result": "", "confidence": 0, "tokens_saved_est": 0}

    classification = classify_task(description)

    if not classification["local_capable"]:
        return {"success": False, "result": "", "confidence": 0, "tokens_saved_est": 0}

    # Build prompt based on task type
    task_type = classification["task_type"]

    if task_type == "summarize":
        result = summarizer.summarize_file(file_content, file_path)
        if result:
            tokens_saved = len(file_content) // 4  # Would have read the whole file
            ledger.record(
                event_type="local_handled",
                tool_name="TaskRouter",
                file_path=file_path,
                original_chars=len(file_content),
                saved_chars=len(file_content) - len(result),
                notes=f"local summarize: {task_type}",
            )
            return {
                "success": True,
                "result": result,
                "confidence": 0.8,
                "tokens_saved_est": tokens_saved,
            }

    elif task_type in ("explain", "review", "find", "list", "classify", "extract"):
        system = "You are a code assistant. Be concise and accurate. Output only the answer."
        prompt = f"{description}\n\nCode:\n{file_content[:6000]}"
        result = summarizer._ollama_generate(prompt, system=system, max_tokens=600)
        if result and len(result) > 20:
            ledger.record(
                event_type="local_handled",
                tool_name="TaskRouter",
                file_path=file_path,
                original_chars=len(file_content) + len(description),
                saved_chars=len(file_content),  # Saved Claude from reading the file
                notes=f"local {task_type}: {description[:60]}",
            )
            return {
                "success": True,
                "result": result,
                "confidence": 0.7,
                "tokens_saved_est": len(file_content) // 4,
            }

    return {"success": False, "result": "", "confidence": 0, "tokens_saved_est": 0}


def _log_classification(description: str, result: dict) -> None:
    """Log classification decision to ledger."""
    ledger.record(
        event_type="task_classify",
        tool_name="TaskRouter",
        original_chars=len(description),
        saved_chars=0,
        notes=f"{result['difficulty']}/{result['task_type']} conf={result['confidence']:.2f}",
    )
