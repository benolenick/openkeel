"""Reasoning step for the bubble pattern (v4 — local LLM + Sonnet CLI + Hyphae)."""

import subprocess
import time

from .config import get_config
from . import ollama as local_llm


def reason_sonnet_cli(task, gathered_data, repo_path=None, hyphae_context=""):
    """Send pre-gathered data + Hyphae context to Sonnet CLI for reasoning.

    Returns (output_text, elapsed_ms).
    """
    cfg = get_config()
    t0 = time.time()

    context_section = ""
    if hyphae_context:
        context_section = (
            "## Project Memory (from Hyphae)\n\n"
            f"{hyphae_context[:4000]}\n\n"
        )

    prompt = (
        "You are a senior engineer analyzing a codebase. "
        "Raw data has been gathered for you.\n\n"
        f"<task>\n{task}\n</task>\n\n"
        f"{context_section}"
        f"## Gathered Data\n\n{gathered_data[:30000]}\n\n"
        "Provide a COMPLETE answer based on the gathered data above. "
        "Cite file paths and line numbers from the gathered data. Be specific. "
        "Do NOT read additional files unless the gathered data is clearly insufficient."
    )

    try:
        r = subprocess.run(
            [
                cfg["claude_bin"],
                "-p",
                "--model",
                cfg["reason_model"],
                "--no-session-persistence",
                "--output-format",
                "text",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            stdin=subprocess.DEVNULL,
        )
        elapsed = round((time.time() - t0) * 1000)
        return r.stdout, elapsed
    except FileNotFoundError:
        raise RuntimeError(
            f"Claude CLI not found at {cfg['claude_bin']}. "
            "Install: npm install -g @anthropic-ai/claude-code"
        )
    except Exception as e:
        elapsed = round((time.time() - t0) * 1000)
        return f"[CLI error: {e}]", elapsed


def reason_local(task, gathered_data, model, repo_path=None, hyphae_context=""):
    """Reason over gathered data using a local LLM via Ollama.

    Returns (output_text, elapsed_ms).
    """
    context_section = ""
    if hyphae_context:
        context_section = (
            "## Project Memory (from Hyphae)\n\n"
            f"{hyphae_context[:4000]}\n\n"
        )

    prompt = (
        f"<task>\n{task}\n</task>\n\n"
        f"{context_section}"
        f"## Gathered Data\n\n{gathered_data[:20000]}\n\n"
        "Provide a COMPLETE answer based on the gathered data above. "
        "Cite file paths and line numbers. Be specific and thorough."
    )

    text, elapsed = local_llm.generate(
        prompt, model,
        system=(
            "You are a senior engineer analyzing a codebase. "
            "Raw data has been pre-gathered for you. Answer based on what you see. "
            "Cite file paths and line numbers. Be specific."
        ),
        max_tokens=2048,
    )

    return text, elapsed


def reason_ultra(task, gathered_data, local_model, repo_path=None, hyphae_context=""):
    """Ultra mode: local LLM reasons, Haiku judges quality and escalates if needed.

    Flow: local reasons → Haiku scores → if poor, Sonnet re-reasons.
    Returns (output_text, elapsed_ms, escalated_to_sonnet).
    """
    from .gather import haiku_api

    t0 = time.time()

    # Step 1: Local LLM reasons
    local_output, local_ms = reason_local(
        task, gathered_data, local_model, repo_path, hyphae_context
    )

    if not local_output or len(local_output.strip()) < 50:
        # Obviously bad — skip to Sonnet
        sonnet_output, sonnet_ms = reason_sonnet_cli(
            task, gathered_data, repo_path, hyphae_context
        )
        elapsed = round((time.time() - t0) * 1000)
        return sonnet_output, elapsed, True

    # Step 2: Haiku judges quality (cheap — ~$0.001)
    judge_result = _haiku_judge(task, local_output)

    # Step 3: If score < 7, escalate to Sonnet
    if judge_result["score"] < 7:
        sonnet_output, sonnet_ms = reason_sonnet_cli(
            task, gathered_data, repo_path, hyphae_context
        )
        elapsed = round((time.time() - t0) * 1000)
        return sonnet_output, elapsed, True

    elapsed = round((time.time() - t0) * 1000)
    return local_output, elapsed, False


def classify_difficulty(task):
    """Haiku classifies task difficulty AND local feasibility. Used by cascade mode.

    Returns dict with difficulty, local_capable, tier, reason, cost.
    The tier decision factors in whether a 5B local model can realistically
    handle the task — not just how hard it is. This avoids the wasteful
    "try local → fail → judge → escalate to Sonnet" pattern.

    Cost: ~$0.0004 (one short Haiku call)
    """
    from .gather import haiku_api
    import json, re

    prompt = (
        f'Classify this code analysis task. Reply with ONLY a JSON object:\n'
        f'{{"difficulty": N, "local_capable": true/false, "reason": "..."}}\n\n'
        f'DIFFICULTY (1-10):\n'
        f'  1-3: Simple lookup (find a value, read a config, what does one function do)\n'
        f'  4-6: Moderate (explain a module, trace a call chain, find a pattern across files)\n'
        f'  7-10: Hard (security audit, architecture review, debug subtle bug, cross-module analysis)\n\n'
        f'LOCAL_CAPABLE — can a small 5B parameter model answer this correctly?\n'
        f'  true:  Single-file tasks. Read one file + extract/summarize. Value lookups. Function explanations.\n'
        f'  false: Cross-file tracing. Multi-module analysis. Anything needing inference across files.\n'
        f'         Architecture questions. Bug diagnosis. Anything where wrong > useless.\n\n'
        f'When in doubt, set local_capable=false. A wasted Sonnet call is cheaper than\n'
        f'a wrong local answer + judge call + Sonnet retry.\n\n'
        f'Task: {task[:500]}'
    )

    result, _, _, cost = haiku_api(
        prompt,
        system='Reply with only a JSON object: {"difficulty": N, "local_capable": true/false, "reason": "..."}. Be conservative with local_capable — false if any doubt.',
        max_tokens=150,
        max_rounds=1,
    )

    difficulty = 5  # default moderate
    local_capable = False  # default to Sonnet (safe)
    reason = ""
    try:
        match = re.search(r'\{[^}]+\}', result or "")
        if match:
            parsed = json.loads(match.group())
            difficulty = int(parsed.get("difficulty", 5))
            local_capable = bool(parsed.get("local_capable", False))
            reason = parsed.get("reason", "")
    except Exception:
        pass

    # Tier decision: local_capable gates whether local is even attempted
    if local_capable and difficulty <= 4:
        tier = "local"  # easy + local can do it → no judge needed
    elif local_capable and difficulty <= 6:
        tier = "local+judge"  # medium + local might do it → judge as safety net
    else:
        tier = "sonnet"  # hard OR local can't do it → straight to Sonnet

    return {
        "difficulty": difficulty, "tier": tier, "reason": reason,
        "cost": cost, "local_capable": local_capable,
    }


def reason_cascade(task, gathered_data, local_model, repo_path=None, hyphae_context=""):
    """Cascade mode: Haiku classifies difficulty + local feasibility, routes smartly.

    Routing logic (local_capable gates everything):
      local_capable=True  + easy (1-4):  local reasons directly (free, no judge)
      local_capable=True  + medium (5-6): local reasons + Haiku judge (safety net)
      local_capable=False OR hard (7+):  Sonnet directly (no wasted local attempt)

    Returns (output_text, elapsed_ms, cascade_info_dict).
    """
    import sys
    t0 = time.time()

    # Step 1: Haiku classifies difficulty + local feasibility (~$0.0004)
    classification = classify_difficulty(task)
    tier = classification["tier"]
    difficulty = classification["difficulty"]
    local_capable = classification.get("local_capable", False)

    cap_tag = "local-capable" if local_capable else "needs-sonnet"
    print(
        f"[bubble] Cascade: difficulty={difficulty}/10, {cap_tag} → {tier} ({classification['reason'][:50]})",
        file=sys.stderr,
    )

    cascade_info = {
        "tier": tier, "difficulty": difficulty,
        "reason": classification["reason"],
        "local_capable": local_capable,
        "classify_cost": classification["cost"],
    }

    if tier == "local":
        # Easy + local-capable — local handles it, no judgment needed
        output, local_ms = reason_local(
            task, gathered_data, local_model, repo_path, hyphae_context
        )
        elapsed = round((time.time() - t0) * 1000)
        cascade_info["escalated"] = False
        return output, elapsed, cascade_info

    elif tier == "local+judge":
        # Medium + local-capable — local reasons, Haiku judges as safety net
        output, reason_ms, escalated = reason_ultra(
            task, gathered_data, local_model, repo_path, hyphae_context
        )
        elapsed = round((time.time() - t0) * 1000)
        cascade_info["escalated"] = escalated
        return output, elapsed, cascade_info

    else:
        # Hard OR local-not-capable — straight to Sonnet, no wasted local attempt
        output, sonnet_ms = reason_sonnet_cli(
            task, gathered_data, repo_path, hyphae_context
        )
        elapsed = round((time.time() - t0) * 1000)
        cascade_info["escalated"] = False
        return output, elapsed, cascade_info


def _haiku_judge(task, output_text):
    """Haiku scores an analysis 1-10. Returns {"score": int, "reason": str, "cost": float}."""
    from .gather import haiku_api
    import json, re

    judge_prompt = (
        f"Rate this code analysis on a 1-10 scale. Reply with ONLY a JSON object: "
        f'{{ "score": N, "reason": "..." }}\n\n'
        f"SCORING CRITERIA (be strict):\n"
        f"- Score 1-3: Wrong, vague, or speculative. Says 'I cannot determine' or guesses without evidence.\n"
        f"- Score 4-6: Partially correct but missing key details, no specific code citations, or uses hedging language like 'likely' or 'inferred'.\n"
        f"- Score 7-8: Correct with specific file paths and code references from the gathered data.\n"
        f"- Score 9-10: Comprehensive, precise, with line numbers and thorough explanation.\n\n"
        f"RED FLAGS (auto-score 3 or below):\n"
        f"- Says 'not explicitly detailed in the provided context'\n"
        f"- Uses 'Inference based on context' instead of citing actual code\n"
        f"- Cannot name the specific function/class/variable asked about\n\n"
        f"Task: {task[:300]}\n\n"
        f"Analysis:\n{output_text[:3000]}"
    )

    judge_result, _, _, judge_cost = haiku_api(
        judge_prompt,
        system='Reply with only a JSON object: {"score": N, "reason": "..."}. Be STRICT. If the analysis hedges, speculates, or lacks code citations, score it below 5.',
        max_tokens=128,
        max_rounds=1,
    )

    score = 4  # default to low — assume bad until proven good
    reason = ""
    try:
        match = re.search(r'\{[^}]+\}', judge_result or "")
        if match:
            parsed = json.loads(match.group())
            score = int(parsed.get("score", 4))
            reason = parsed.get("reason", "")
    except Exception:
        pass

    return {"score": score, "reason": reason, "cost": judge_cost}


def run_vanilla(task, repo_path):
    """Vanilla Sonnet CLI — direct, no bubble. Fallback path."""
    cfg = get_config()
    t0 = time.time()
    try:
        r = subprocess.run(
            [
                cfg["claude_bin"],
                "-p",
                "--model",
                cfg["reason_model"],
                "--no-session-persistence",
                "--output-format",
                "text",
                f"{task} The repo is at {repo_path}. Read whatever files you need.",
            ],
            capture_output=True,
            text=True,
            timeout=600,
            stdin=subprocess.DEVNULL,
        )
        elapsed = round((time.time() - t0) * 1000)
        return r.stdout, elapsed
    except Exception as e:
        elapsed = round((time.time() - t0) * 1000)
        return f"[CLI error: {e}]", elapsed
