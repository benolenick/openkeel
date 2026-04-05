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

OLLAMA_URL = os.environ.get("TOKEN_SAVER_OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("TOKEN_SAVER_MODEL", "qwen2.5-coder:3b")
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
            return data.get("response", "") or data.get("thinking", "")
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
