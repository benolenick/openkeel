"""LLM Calibrator — tests the local LLM's competency across task types.

Runs a battery of standardized tests against the local model, scores it
per-capability, and stores results. Token saver engines check trust scores
before delegating work.

Test categories:
  1. Summarization — can it compress text while retaining key facts?
  2. Extraction — can it pull specific data from noisy output?
  3. Classification — can it categorize correctly?
  4. Code understanding — can it identify functions/classes/bugs?
  5. Instruction following — does it respect output format constraints?

Results are cached per model name in ~/.openkeel/llm_calibration.json.
Re-run when the model changes or on demand.

Usage:
    from openkeel.token_saver.engines.llm_calibrator import get_trust_scores, calibrate
    scores = get_trust_scores()  # returns cached or runs calibration
    if scores["summarization"] >= 0.7:
        # safe to delegate summarization
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

CACHE_PATH = Path.home() / ".openkeel" / "llm_calibration.json"
# Re-calibrate if cache is older than 7 days
CACHE_TTL_SECONDS = 7 * 24 * 3600


def _get_model_name() -> str:
    """Get current local LLM model name."""
    return os.environ.get("TOKEN_SAVER_MODEL", "gemma4:e2b")


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def get_trust_scores() -> dict[str, float]:
    """Get trust scores for current model. Returns cached if fresh, else calibrates."""
    model = _get_model_name()
    cache = _load_cache()

    if model in cache:
        entry = cache[model]
        age = time.time() - entry.get("timestamp", 0)
        if age < CACHE_TTL_SECONDS:
            return entry.get("scores", _default_scores())

    # Need to calibrate
    result = calibrate()
    return result.get("scores", _default_scores())


def _default_scores() -> dict[str, float]:
    """Conservative defaults — assume model can't do much."""
    return {
        "summarization": 0.3,
        "extraction": 0.3,
        "classification": 0.3,
        "code_understanding": 0.3,
        "instruction_following": 0.3,
        "overall": 0.3,
    }


# ══════════════════════════════════════════════════════════════════
# TEST BATTERY
# ══════════════════════════════════════════════════════════════════

_SUMMARIZATION_TESTS = [
    {
        "name": "process_list_summary",
        "input": (
            "USER PID %CPU %MEM COMMAND\n"
            "root 1 0.0 0.0 /sbin/init\n"
            "root 2 0.0 0.0 [kthreadd]\n"
            "root 3 0.0 0.0 [pool_workqueue]\n"
            "om 1234 45.2 3.1 python3 train_model.py\n"
            "om 5678 22.1 1.5 node server.js\n"
            "om 9012 0.5 0.1 vim config.yaml\n"
            "root 100 0.0 0.0 [migration/0]\n"
            "root 101 0.0 0.0 [migration/1]\n"
            "root 102 0.0 0.0 [ksoftirqd/0]\n"
            "nobody 200 0.0 0.0 /usr/sbin/dnsmasq\n"
        ),
        "prompt": "Summarize this process listing. Keep only processes using >0.1% CPU.",
        "must_contain": ["train_model", "server.js", "45.2", "22.1"],
        "must_not_contain": ["kthreadd", "migration", "ksoftirqd"],
    },
    {
        "name": "log_summary",
        "input": (
            "2026-04-06 10:00:01 INFO Health check OK\n"
            "2026-04-06 10:00:02 INFO Health check OK\n"
            "2026-04-06 10:00:03 INFO Health check OK\n"
            "2026-04-06 10:00:04 ERROR Connection refused: database at 5432\n"
            "2026-04-06 10:00:05 INFO Health check OK\n"
            "2026-04-06 10:00:06 WARNING Disk usage at 89%\n"
            "2026-04-06 10:00:07 INFO Health check OK\n"
            "2026-04-06 10:00:08 INFO Health check OK\n"
        ),
        "prompt": "Summarize these logs. Focus on errors and warnings, skip repeated info lines.",
        "must_contain": ["Connection refused", "database", "5432", "Disk", "89%"],
        "must_not_contain": [],
    },
]

_EXTRACTION_TESTS = [
    {
        "name": "extract_error_line",
        "input": (
            "Building project...\n"
            "Compiling src/main.rs\n"
            "Compiling src/utils.rs\n"
            "error[E0308]: mismatched types at src/main.rs:42\n"
            "  expected `String`, found `&str`\n"
            "Compiling src/config.rs\n"
            "Build failed.\n"
        ),
        "prompt": "What is the error? Output only the error type, file, and line number.",
        "must_contain": ["E0308", "main.rs", "42"],
        "must_not_contain": [],
    },
    {
        "name": "extract_count",
        "input": (
            "Package                    Version\n"
            "-------------------------- -------\n"
            "numpy                      1.24.0\n"
            "pandas                     2.0.0\n"
            "requests                   2.31.0\n"
            "flask                      3.0.0\n"
            "django                     5.0.0\n"
        ),
        "prompt": "How many packages are listed? Answer with just the number.",
        "must_contain": ["5"],
        "must_not_contain": [],
    },
]

_CLASSIFICATION_TESTS = [
    {
        "name": "classify_error_vs_info",
        "input": "error[E0308]: mismatched types at src/main.rs:42",
        "prompt": "Is this an ERROR, WARNING, or INFO message? Answer with one word.",
        "must_contain_any": ["ERROR", "error", "Error"],
        "must_not_contain": [],
    },
    {
        "name": "classify_language",
        "input": "def hello():\n    print('hello world')\n\nhello()",
        "prompt": "What programming language is this? Answer with one word.",
        "must_contain_any": ["Python", "python", "PYTHON"],
        "must_not_contain": [],
    },
]

_CODE_TESTS = [
    {
        "name": "identify_function",
        "input": (
            "import os\n\n"
            "class UserService:\n"
            "    def __init__(self, db):\n"
            "        self.db = db\n\n"
            "    def get_user(self, user_id):\n"
            "        return self.db.query(user_id)\n\n"
            "    def delete_user(self, user_id):\n"
            "        self.db.delete(user_id)\n"
        ),
        "prompt": "List the method names in this class. Output only the names, one per line.",
        "must_contain": ["get_user", "delete_user", "__init__"],
        "must_not_contain": [],
    },
    {
        "name": "spot_bug",
        "input": (
            "def divide(a, b):\n"
            "    return a / b\n\n"
            "result = divide(10, 0)\n"
        ),
        "prompt": "Is there a bug? If so, what is it? One sentence answer.",
        "must_contain_any": ["division by zero", "divide by zero", "ZeroDivision", "zero"],
        "must_not_contain": [],
    },
]

_INSTRUCTION_TESTS = [
    {
        "name": "json_output",
        "input": "The server is running on port 8080 with 4 workers.",
        "prompt": 'Extract port and workers as JSON. Output ONLY valid JSON like {"port": N, "workers": N}',
        "validate_json": True,
        "must_contain": ["8080", "4"],
        "must_not_contain": [],
    },
    {
        "name": "line_limit",
        "input": "apple banana cherry date elderberry fig grape honeydew kiwi lemon",
        "prompt": "List these fruits, one per line. Output EXACTLY 3 lines, no more.",
        "max_lines": 5,  # Allow some slack, but not 10
        "must_not_contain": [],
    },
]


def _run_test(test: dict, category: str) -> tuple[float, str]:
    """Run a single test. Returns (score 0-1, details)."""
    from openkeel.token_saver.summarizer import ollama_generate

    full_input = test["input"]
    prompt = f"{test['prompt']}\n\nInput:\n{full_input}"

    start = time.time()
    result = ollama_generate(prompt, max_tokens=300)
    elapsed = time.time() - start

    if not result:
        return 0.0, f"FAIL: no response ({elapsed:.1f}s)"

    score = 0.0
    details = []

    # Check must_contain
    must_contain = test.get("must_contain", [])
    if must_contain:
        found = sum(1 for kw in must_contain if kw.lower() in result.lower())
        contain_score = found / len(must_contain)
        score += contain_score * 0.4
        if found < len(must_contain):
            missing = [kw for kw in must_contain if kw.lower() not in result.lower()]
            details.append(f"missing: {missing}")

    # Check must_contain_any
    must_any = test.get("must_contain_any", [])
    if must_any:
        found_any = any(kw.lower() in result.lower() for kw in must_any)
        score += 0.4 if found_any else 0.0
        if not found_any:
            details.append(f"none of {must_any} found")
    elif not must_contain:
        score += 0.4  # No contain check = full marks

    # Check must_not_contain
    must_not = test.get("must_not_contain", [])
    if must_not:
        bad = [kw for kw in must_not if kw.lower() in result.lower()]
        score += 0.0 if bad else 0.3
        if bad:
            details.append(f"unwanted: {bad}")
    else:
        score += 0.3

    # Check JSON validity
    if test.get("validate_json"):
        try:
            parsed = result.strip()
            if parsed.startswith("```"):
                parsed = "\n".join(l for l in parsed.split("\n") if not l.strip().startswith("```"))
            start_idx = parsed.find("{")
            end_idx = parsed.rfind("}") + 1
            if start_idx >= 0 and end_idx > start_idx:
                json.loads(parsed[start_idx:end_idx])
                score += 0.2
            else:
                details.append("no JSON found")
        except json.JSONDecodeError:
            details.append("invalid JSON")

    # Check line limit
    max_lines = test.get("max_lines")
    if max_lines:
        actual = len(result.strip().split("\n"))
        if actual <= max_lines:
            score += 0.1
        else:
            details.append(f"too many lines: {actual} > {max_lines}")

    # Response time bonus (under 5s = good)
    if elapsed < 5:
        score += 0.1
    elif elapsed > 15:
        score -= 0.1

    score = max(0.0, min(1.0, score))
    detail_str = f"{'PASS' if score >= 0.6 else 'FAIL'} ({score:.2f}, {elapsed:.1f}s)"
    if details:
        detail_str += f" [{', '.join(details)}]"

    return score, detail_str


def calibrate() -> dict:
    """Run full calibration battery. Returns scores dict and caches result."""
    from openkeel.token_saver.summarizer import is_available

    model = _get_model_name()
    print(f"[LLM Calibrator] Testing model: {model}")

    if not is_available():
        print("[LLM Calibrator] Ollama not available — using conservative defaults")
        result = {
            "model": model,
            "timestamp": time.time(),
            "scores": _default_scores(),
            "available": False,
            "tests": {},
        }
        cache = _load_cache()
        cache[model] = result
        _save_cache(cache)
        return result

    all_tests = {
        "summarization": _SUMMARIZATION_TESTS,
        "extraction": _EXTRACTION_TESTS,
        "classification": _CLASSIFICATION_TESTS,
        "code_understanding": _CODE_TESTS,
        "instruction_following": _INSTRUCTION_TESTS,
    }

    scores = {}
    test_details = {}
    total_time = 0

    for category, tests in all_tests.items():
        cat_scores = []
        cat_details = []

        for test in tests:
            score, detail = _run_test(test, category)
            cat_scores.append(score)
            cat_details.append(f"  {test['name']}: {detail}")
            print(f"  [{category}] {test['name']}: {detail}")

        avg = sum(cat_scores) / len(cat_scores) if cat_scores else 0
        scores[category] = round(avg, 3)
        test_details[category] = cat_details

    # Overall score — weighted average
    weights = {
        "summarization": 0.30,      # Most important for token saver
        "extraction": 0.25,         # Second most — pulling data from output
        "instruction_following": 0.20,  # Must respect format constraints
        "classification": 0.15,
        "code_understanding": 0.10,
    }
    overall = sum(scores.get(k, 0) * w for k, w in weights.items())
    scores["overall"] = round(overall, 3)

    # Trust level label
    if overall >= 0.8:
        trust = "HIGH — safe to delegate most summarization and extraction"
    elif overall >= 0.6:
        trust = "MEDIUM — safe for summarization, cautious on extraction"
    elif overall >= 0.4:
        trust = "LOW — only simple compression, no LLM summarization"
    else:
        trust = "MINIMAL — disable LLM engines, use rule-based only"

    print(f"\n[LLM Calibrator] Results for {model}:")
    for cat, score in scores.items():
        print(f"  {cat:25s}: {score:.3f}")
    print(f"  Trust level: {trust}")

    result = {
        "model": model,
        "timestamp": time.time(),
        "scores": scores,
        "trust_level": trust,
        "available": True,
        "tests": test_details,
    }

    cache = _load_cache()
    cache[model] = result
    _save_cache(cache)

    return result


def should_use_llm(task_type: str, min_score: float = 0.6) -> bool:
    """Quick check: should we use the local LLM for this task type?

    Args:
        task_type: one of summarization, extraction, classification,
                   code_understanding, instruction_following
        min_score: minimum trust score required (default 0.6)

    Returns:
        True if the model scored >= min_score on this task type.
    """
    scores = get_trust_scores()
    return scores.get(task_type, 0) >= min_score


if __name__ == "__main__":
    result = calibrate()
    print(f"\nScores: {json.dumps(result['scores'], indent=2)}")
