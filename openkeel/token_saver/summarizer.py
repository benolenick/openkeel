"""Local LLM summarizer for token saver — talks to Ollama via SSH tunnel.

Provides file summarization, output filtering, and task classification
using a local model (qwen3:8b on jagg, tunneled to localhost:11447).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

def _resolve_fast_endpoint() -> tuple[str, str]:
    """Resolve (url, model) from gpu_tier.get_fast_endpoint() — picks the
    fastest small model across all reachable ollama instances.

    Env vars TOKEN_SAVER_OLLAMA_URL and TOKEN_SAVER_MODEL override detection.
    Falls back to localhost:11434 + gemma4:e2b on any failure.
    """
    env_url = os.environ.get("TOKEN_SAVER_OLLAMA_URL")
    env_model = os.environ.get("TOKEN_SAVER_MODEL")
    if env_url and env_model:
        return env_url, env_model
    try:
        from openkeel.token_saver.engines.gpu_tier import get_fast_endpoint
        fast = get_fast_endpoint()
        if fast is not None:
            url, model, _ = fast
            return (env_url or url), (env_model or model)
    except Exception:
        pass
    return (env_url or "http://127.0.0.1:11434"), (env_model or "gemma4:e2b")


OLLAMA_URL, MODEL = _resolve_fast_endpoint()
TIMEOUT = int(os.environ.get("TOKEN_SAVER_TIMEOUT", "30"))


def ollama_generate(prompt: str, system: str = "", max_tokens: int = 512) -> str:
    """Send a generate request to Ollama. Returns empty string on failure."""
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    payload = json.dumps({
        "model": MODEL,
        "prompt": full_prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.1,
            "num_predict": max_tokens,
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("response", "")
    except Exception:
        return ""


def is_available() -> bool:
    """Check if the Ollama endpoint is reachable."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def summarize_file(content: str, file_path: str = "", max_lines: int = 15) -> str:
    """Summarize a source file to ~15 lines."""
    ext = file_path.rsplit(".", 1)[-1] if "." in file_path else ""

    # Extract structure hints for the model
    lines = content.split("\n")
    line_count = len(lines)

    # Quick structural extraction: classes, functions, top-level names
    structure_hints = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("class ", "def ", "async def ")):
            structure_hints.append(f"  L{i+1}: {stripped[:100]}")
        if len(structure_hints) >= 30:
            break

    structure_block = "\n".join(structure_hints[:25]) if structure_hints else "(no classes/functions found)"

    system = (
        "You are a code summarizer. Output ONLY plain text, NO code blocks, NO markdown fences. "
        "Be extremely terse. Use bullet points."
    )
    prompt = (
        f"Summarize this {ext} file ({line_count} lines) in {max_lines} bullet points.\n"
        f"File: {file_path}\n\n"
        f"Structure:\n{structure_block}\n\n"
        f"First 120 lines:\n{chr(10).join(lines[:120])}\n"
    )
    result = ollama_generate(prompt, system=system, max_tokens=400)
    if not result:
        return ""
    # Strip any markdown fences the model might still emit
    result = result.strip()
    if result.startswith("```"):
        result = "\n".join(
            line for line in result.split("\n")
            if not line.strip().startswith("```")
        )
    return result.strip()


def filter_output(command: str, output: str, max_lines: int = 30) -> str:
    """Filter command output to only the relevant parts."""
    if len(output) < 500:
        return output  # Already small enough

    system = (
        "You are an output filter. Extract ONLY the useful information. "
        "Remove noise, repetition, boilerplate. Output the filtered result directly."
    )
    prompt = (
        f"Command: {command}\n\n"
        f"Filter this output to the most relevant {max_lines} lines:\n\n"
        f"{output[:6000]}"
    )
    result = ollama_generate(prompt, system=system, max_tokens=600)
    return result.strip() if result else output


def classify_task(description: str) -> dict[str, Any]:
    """Classify a task's difficulty and type for routing decisions."""
    system = (
        "You are a task classifier. Respond with ONLY valid JSON, no other text. "
        'Format: {"difficulty": "trivial|moderate|complex", "type": "summarize|classify|review|edit|debug|architect|research", "local_capable": true/false, "reason": "brief reason"}'
    )
    prompt = f"Classify this coding task:\n{description[:2000]}"
    result = ollama_generate(prompt, system=system, max_tokens=150)
    if not result:
        return {"difficulty": "complex", "local_capable": False, "reason": "llm unavailable"}

    # Parse JSON from response
    try:
        return json.loads(result.strip())
    except json.JSONDecodeError:
        # Try extracting JSON
        start = result.find("{")
        end = result.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(result[start:end + 1])
            except json.JSONDecodeError:
                pass
    return {"difficulty": "complex", "local_capable": False, "reason": "parse error"}


def summarize_bash_output(command: str, output: str, max_lines: int = 20) -> str:
    """LLM-summarize large bash output to only the actionable information."""
    if len(output) < 800:
        return ""  # Too small to bother

    system = (
        "You are an output summarizer for a developer's CLI session. "
        "Extract the IMPORTANT lines — errors, warnings, key results, active processes, counts, paths. "
        "Drop boilerplate, headers, repeated patterns, but KEEP substantive entries. "
        "For process listings: keep all processes using >0.5% CPU or >1% memory. "
        "For service listings: keep all running/failed services. "
        "For file listings: keep files sorted by size, largest first. "
        "Output plain text, no markdown fences. You MUST output at least {min_lines} lines."
    )
    min_lines = max(15, max_lines)

    # Pre-sort process listings so high-CPU/memory entries appear first in the LLM window
    feed = output
    cmd_lower = command.lower().strip()
    if cmd_lower.startswith(("ps aux", "ps -")):
        lines = output.strip().split("\n")
        header = lines[0] if lines else ""
        procs = lines[1:]
        def _cpu_key(line):
            try:
                return -float(line.split()[2])
            except (IndexError, ValueError):
                return 0
        procs.sort(key=_cpu_key)
        feed = header + "\n" + "\n".join(procs)

    prompt = (
        f"Command: {command}\n"
        f"Output ({len(output)} chars, {len(output.splitlines())} lines):\n\n"
        f"{feed[:5000]}\n\n"
        f"Extract the {max_lines} most important lines. You MUST include at least {min_lines} lines. "
        f"Keep exact numbers, paths, error messages. Do NOT over-compress."
    )
    result = ollama_generate(prompt, system=system, max_tokens=800)
    return result.strip() if result else ""


def summarize_grep_results(pattern: str, output: str, file_context: str = "",
                           max_lines: int = 25) -> str:
    """LLM-summarize grep results, grouping by file and highlighting key matches."""
    if len(output) < 1000:
        return ""

    system = (
        "You are a search result summarizer. Group matches by file. "
        "For each file, show the most important matches (not all). "
        "If a file was already read, just show line numbers. "
        "Output plain text, no markdown fences."
    )
    ctx = f"\nAlready-read files: {file_context}\n" if file_context else ""
    prompt = (
        f"Grep pattern: {pattern}\n{ctx}"
        f"Results ({len(output.splitlines())} matches):\n\n"
        f"{output[:6000]}\n\n"
        f"Summarize to {max_lines} lines. Group by file. For already-read files, just list line numbers."
    )
    result = ollama_generate(prompt, system=system, max_tokens=600)
    return result.strip() if result else ""


def summarize_file_reread(content: str, file_path: str = "", prev_summary: str = "",
                          max_lines: int = 20) -> str:
    """Richer re-read summary — includes key constants, config values, and structural overview."""
    ext = file_path.rsplit(".", 1)[-1] if "." in file_path else ""
    lines = content.split("\n")
    line_count = len(lines)

    # Extract richer structural hints
    structure = []
    constants = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("class ", "def ", "async def ")):
            indent = len(line) - len(line.lstrip())
            prefix = "  " if indent > 0 else ""
            structure.append(f"{prefix}L{i+1}: {stripped[:100]}")
        elif "=" in stripped and stripped[0].isupper() and not stripped.startswith(("#", "/", "*")):
            # Likely a constant
            if len(constants) < 10:
                constants.append(f"  L{i+1}: {stripped[:80]}")
        if len(structure) >= 40:
            break

    system = (
        "You are a code summarizer doing a RE-READ summary. The developer has read this file before. "
        "Focus on: overall purpose, key classes/functions with their roles, important constants/config, "
        "and anything that would help them remember what's in the file without re-reading it. "
        "Output plain text bullet points, no markdown fences. Be terse but complete."
    )
    const_block = "\n".join(constants) if constants else "(none found)"
    struct_block = "\n".join(structure[:30]) if structure else "(none found)"

    prompt = (
        f"Re-summarize this {ext} file ({line_count} lines) in {max_lines} bullet points.\n"
        f"File: {file_path}\n\n"
        f"Constants/Config:\n{const_block}\n\n"
        f"Structure:\n{struct_block}\n\n"
        f"First 100 lines:\n{chr(10).join(lines[:100])}\n"
    )
    result = ollama_generate(prompt, system=system, max_tokens=500)
    if not result:
        return ""
    result = result.strip()
    if result.startswith("```"):
        result = "\n".join(l for l in result.split("\n") if not l.strip().startswith("```"))
    return result.strip()


def extract_relevant_lines(content: str, query: str, max_lines: int = 50) -> str:
    """Extract only the lines relevant to a query from file content."""
    if len(content.split("\n")) <= max_lines:
        return content

    system = (
        "You are a code extractor. Output ONLY the relevant lines with their "
        "line numbers. No explanation."
    )
    prompt = (
        f"Query: {query}\n\n"
        f"Extract the lines relevant to this query (max {max_lines} lines):\n\n"
        f"{content[:8000]}"
    )
    result = ollama_generate(prompt, system=system, max_tokens=800)
    return result.strip() if result else content[:max_lines * 80]
