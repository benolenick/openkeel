"""Task Router — LLM-powered task classification + local handling.

Replaces regex pattern matching with a 1-shot LLM classifier (from RouteLLM's
insight). Asks the local qwen2.5-coder:3b to rate task complexity 1-5, which
is far more accurate than regex for edge cases.

Falls back to regex patterns if the LLM is unavailable.

All routing decisions are logged to the ledger.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from openkeel.token_saver import summarizer, ledger

# Cache recent classifications to avoid re-classifying the same task
_classify_cache: dict[str, dict] = {}
_CACHE_TTL = 300  # 5 minutes

# Fallback regex patterns (used when LLM is unavailable)
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

# 1-shot classification prompt
_CLASSIFY_PROMPT = """\
Rate this coding task's complexity from 1 to 5. Reply with ONLY a JSON object.

Scale:
1 = trivial: add import, rename variable, fix typo, format code, sort imports
2 = simple: explain a function, summarize a file, list all classes, find usages
3 = moderate: review a diff, write a unit test, refactor a single function, add error handling
4 = complex: debug a multi-file issue, design an API, refactor across modules, optimize performance
5 = expert: architecture decisions, security audit, complex algorithm design, system-wide refactoring

Task: {task}

Reply ONLY with: {{"score": N, "type": "brief_type", "reason": "5 words max"}}"""


def classify_task(description: str) -> dict[str, Any]:
    """Classify a task using LLM (preferred) or regex fallback.

    Returns:
        {
            "difficulty": "trivial" | "simple" | "moderate" | "complex" | "expert",
            "score": 1-5,
            "task_type": str,
            "local_capable": bool,
            "confidence": float,
            "method": "llm" | "regex" | "heuristic",
        }
    """
    # Check cache
    cache_key = description[:200].strip().lower()
    if cache_key in _classify_cache:
        cached = _classify_cache[cache_key]
        if time.time() - cached.get("_cached_at", 0) < _CACHE_TTL:
            return cached

    # Try LLM classification first
    result = _llm_classify(description)
    if result:
        result["method"] = "llm"
        _log_classification(description, result)
        result["_cached_at"] = time.time()
        _classify_cache[cache_key] = result
        return result

    # Fallback to regex
    result = _regex_classify(description)
    result["method"] = "regex"
    _log_classification(description, result)
    result["_cached_at"] = time.time()
    _classify_cache[cache_key] = result
    return result


def _llm_classify(description: str) -> dict[str, Any] | None:
    """Use the local LLM to classify task complexity."""
    if not summarizer.is_available():
        return None

    prompt = _CLASSIFY_PROMPT.format(task=description[:500])
    raw = summarizer._ollama_generate(prompt, max_tokens=80)
    if not raw:
        return None

    # Parse the response
    parsed = _parse_score_response(raw)
    if not parsed:
        return None

    score = parsed["score"]
    difficulty_map = {1: "trivial", 2: "simple", 3: "moderate", 4: "complex", 5: "expert"}
    difficulty = difficulty_map.get(score, "moderate")

    # Local capable: scores 1-2 fully, 3 partially
    local_capable = score <= 2
    confidence = 0.85 if score in (1, 2, 4, 5) else 0.65  # extremes are easier to classify

    return {
        "difficulty": difficulty,
        "score": score,
        "task_type": parsed.get("type", "general"),
        "local_capable": local_capable,
        "confidence": confidence,
        "reason": parsed.get("reason", ""),
    }


def _parse_score_response(raw: str) -> dict | None:
    """Parse the LLM's JSON response, handling common format issues."""
    raw = raw.strip()

    # Try direct JSON parse
    try:
        data = json.loads(raw)
        if "score" in data and isinstance(data["score"], (int, float)):
            data["score"] = max(1, min(5, int(data["score"])))
            return data
    except json.JSONDecodeError:
        pass

    # Try extracting JSON from markdown/text
    json_match = re.search(r'\{[^}]+\}', raw)
    if json_match:
        try:
            data = json.loads(json_match.group())
            if "score" in data:
                data["score"] = max(1, min(5, int(data["score"])))
                return data
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # Last resort: extract just a number
    num_match = re.search(r'\b([1-5])\b', raw)
    if num_match:
        return {"score": int(num_match.group(1)), "type": "general", "reason": "number_only"}

    return None


def _regex_classify(description: str) -> dict[str, Any]:
    """Fallback regex-based classification."""
    desc_lower = description.lower().strip()

    for pattern, task_type in _TRIVIAL_PATTERNS:
        if pattern.search(desc_lower):
            return {
                "difficulty": "trivial",
                "score": 1,
                "task_type": task_type,
                "local_capable": True,
                "confidence": 0.9,
            }

    for pattern, task_type in _MODERATE_PATTERNS:
        if pattern.search(desc_lower):
            return {
                "difficulty": "simple",
                "score": 2,
                "task_type": task_type,
                "local_capable": True,
                "confidence": 0.7,
            }

    # Heuristic fallback
    complexity_signals = 0
    if len(description) > 500:
        complexity_signals += 1
    if any(w in desc_lower for w in ("refactor", "architect", "design", "debug", "investigate")):
        complexity_signals += 2
    if any(w in desc_lower for w in ("multiple files", "across", "codebase", "all files")):
        complexity_signals += 1
    if any(w in desc_lower for w in ("why", "how should", "what's the best", "trade-off")):
        complexity_signals += 2

    if complexity_signals >= 3:
        return {"difficulty": "complex", "score": 4, "task_type": "complex", "local_capable": False, "confidence": 0.6}
    elif complexity_signals >= 1:
        return {"difficulty": "moderate", "score": 3, "task_type": "general", "local_capable": False, "confidence": 0.5}
    else:
        return {"difficulty": "simple", "score": 2, "task_type": "general", "local_capable": True, "confidence": 0.5}


def attempt_local(
    description: str,
    file_content: str = "",
    file_path: str = "",
) -> dict[str, Any]:
    """Attempt to handle a task locally. Returns result or failure."""
    if not summarizer.is_available():
        return {"success": False, "result": "", "confidence": 0, "tokens_saved_est": 0}

    classification = classify_task(description)

    if not classification["local_capable"]:
        return {"success": False, "result": "", "confidence": 0, "tokens_saved_est": 0}

    task_type = classification["task_type"]

    if task_type in ("summarize",):
        result = summarizer.summarize_file(file_content, file_path)
        if result:
            tokens_saved = len(file_content) // 4
            ledger.record(
                event_type="local_handled",
                tool_name="TaskRouter",
                file_path=file_path,
                original_chars=len(file_content),
                saved_chars=len(file_content) - len(result),
                notes=f"local {task_type} [llm-classified score={classification['score']}]",
            )
            return {"success": True, "result": result, "confidence": 0.8, "tokens_saved_est": tokens_saved}

    elif classification["score"] <= 2:
        system = "You are a code assistant. Be concise and accurate. Output only the answer."
        prompt = f"{description}\n\nCode:\n{file_content[:6000]}"
        result = summarizer._ollama_generate(prompt, system=system, max_tokens=600)
        if result and len(result) > 20:
            ledger.record(
                event_type="local_handled",
                tool_name="TaskRouter",
                file_path=file_path,
                original_chars=len(file_content) + len(description),
                saved_chars=len(file_content),
                notes=f"local {task_type} [score={classification['score']}, method={classification['method']}]",
            )
            return {"success": True, "result": result, "confidence": 0.7, "tokens_saved_est": len(file_content) // 4}

    return {"success": False, "result": "", "confidence": 0, "tokens_saved_est": 0}


def _log_classification(description: str, result: dict) -> None:
    ledger.record(
        event_type="task_classify",
        tool_name="TaskRouter",
        original_chars=len(description),
        saved_chars=0,
        notes=f"score={result.get('score', '?')}/{result['difficulty']} "
              f"method={result.get('method', '?')} local={result.get('local_capable', '?')} "
              f"conf={result.get('confidence', 0):.2f}",
    )
