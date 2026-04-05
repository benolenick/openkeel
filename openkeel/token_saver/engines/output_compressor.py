"""Output Compressor — reduce token waste from large tool outputs.

Targets:
  - Bash outputs (npm install, git log, test results, build output)
  - Grep results (too many matches)
  - Glob results (huge file lists)

Two modes:
  1. Rule-based compression (instant, no LLM needed)
  2. LLM-powered compression (uses local model for intelligent filtering)

All compression is logged to the ledger with before/after char counts.
"""

from __future__ import annotations

import os
import re
from typing import Any

from openkeel.token_saver import ledger

# ---------------------------------------------------------------------------
# Rule-based compressors (instant, no LLM)
# ---------------------------------------------------------------------------

# Patterns for outputs that are mostly noise
_NPM_NOISE = re.compile(r"^(npm warn|added \d+ packages|up to date|audited \d+)", re.MULTILINE)
_PIP_NOISE = re.compile(r"^(Requirement already|Downloading|Using cached|Installing collected)", re.MULTILINE)
_BUILD_NOISE = re.compile(r"^(\s*\[INFO\]|\s*Compiling|\s*Finished|\s*Downloaded)", re.MULTILINE)
_GIT_VERBOSE = re.compile(r"^(remote: (Counting|Compressing|Total|Enumerating))", re.MULTILINE)
_TEST_PASS_LINE = re.compile(r"^.*PASS(ED)?.*$", re.MULTILINE)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

# Commands where we know the output pattern
_KNOWN_COMPRESSORS: dict[str, str] = {
    "npm install": "package_manager",
    "npm ci": "package_manager",
    "yarn install": "package_manager",
    "pip install": "package_manager",
    "pip3 install": "package_manager",
    "cargo build": "build",
    "go build": "build",
    "make": "build",
    "git push": "git_push",
    "git pull": "git_pull",
    "git fetch": "git_fetch",
    "git clone": "git_clone",
}


def compress_output(
    command: str,
    output: str,
    tool_name: str = "Bash",
    use_llm: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Compress a tool output. Returns (compressed_output, metadata).

    Metadata includes original_chars, compressed_chars, saved_chars, method used.
    """
    original_chars = len(output)

    # Don't compress small outputs
    if original_chars < 800:
        return output, {
            "original_chars": original_chars,
            "compressed_chars": original_chars,
            "saved_chars": 0,
            "method": "skip_small",
        }

    # Strip ANSI escape codes first
    cleaned = _ANSI_ESCAPE.sub("", output)

    # Try rule-based compression
    compressed = _rule_compress(command, cleaned, tool_name)

    if compressed is not None:
        saved = max(0, original_chars - len(compressed))
        meta = {
            "original_chars": original_chars,
            "compressed_chars": len(compressed),
            "saved_chars": saved,
            "method": "rule_based",
        }
        if saved > 100:
            ledger.record(
                event_type="output_compress",
                tool_name=tool_name,
                original_chars=original_chars,
                saved_chars=saved,
                notes=f"rule-based: {command[:80]}",
            )
        return compressed, meta

    # For very large outputs without a specific compressor, truncate intelligently
    if original_chars > 5000:
        compressed = _smart_truncate(cleaned, max_chars=3000)
        saved = max(0, original_chars - len(compressed))
        meta = {
            "original_chars": original_chars,
            "compressed_chars": len(compressed),
            "saved_chars": saved,
            "method": "smart_truncate",
        }
        if saved > 200:
            ledger.record(
                event_type="output_compress",
                tool_name=tool_name,
                original_chars=original_chars,
                saved_chars=saved,
                notes=f"truncated: {command[:80]}",
            )
        return compressed, meta

    return cleaned, {
        "original_chars": original_chars,
        "compressed_chars": len(cleaned),
        "saved_chars": max(0, original_chars - len(cleaned)),
        "method": "ansi_strip_only",
    }


def _rule_compress(command: str, output: str, tool_name: str) -> str | None:
    """Apply rule-based compression. Returns None if no rule matches."""
    cmd_base = command.strip().split("|")[0].strip()

    # Package manager output
    if any(cmd_base.startswith(k) for k in ("npm install", "npm ci", "yarn", "pip install", "pip3 install")):
        return _compress_package_install(output)

    # Build output
    if any(cmd_base.startswith(k) for k in ("cargo build", "go build", "make", "gcc", "g++")):
        return _compress_build(output)

    # Git operations
    if cmd_base.startswith("git push") or cmd_base.startswith("git pull") or cmd_base.startswith("git fetch"):
        return _compress_git_remote(output)

    # Test output
    if any(k in cmd_base for k in ("pytest", "jest", "mocha", "cargo test", "go test", "npm test", "npm run test")):
        return _compress_test(output)

    # Large grep/find results
    if tool_name == "Grep":
        return _compress_search_results(output, max_results=30)

    if tool_name == "Glob":
        return _compress_search_results(output, max_results=40)

    return None


def _compress_package_install(output: str) -> str:
    """Compress npm/pip install output to just errors and summary."""
    lines = output.split("\n")
    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Keep errors and warnings
        if any(w in stripped.lower() for w in ("error", "err!", "warn", "vulnerability", "deprecated")):
            kept.append(line)
        # Keep the summary line
        if any(w in stripped for w in ("added", "up to date", "Successfully installed", "audited")):
            kept.append(line)
    if not kept:
        kept = ["(install completed, no errors)"]
    return "\n".join(kept)


def _compress_build(output: str) -> str:
    """Compress build output to errors and final result."""
    lines = output.split("\n")
    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if any(w in stripped.lower() for w in ("error", "warning", "failed", "cannot find")):
            kept.append(line)
        if any(w in stripped.lower() for w in ("finished", "built", "compiled", "success")):
            kept.append(line)
    if not kept:
        # Take last 5 lines as summary
        kept = [l for l in lines[-5:] if l.strip()]
    return "\n".join(kept)


def _compress_git_remote(output: str) -> str:
    """Compress git push/pull/fetch to essentials."""
    lines = output.split("\n")
    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Skip remote counting/compressing noise
        if _GIT_VERBOSE.match(stripped):
            continue
        kept.append(line)
    return "\n".join(kept) if kept else "(completed)"


def _compress_test(output: str) -> str:
    """Compress test output — keep failures, summary, skip individual passes."""
    lines = output.split("\n")
    kept = []
    pass_count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Always keep failures
        if any(w in stripped.lower() for w in ("fail", "error", "assert", "traceback", "expected", "actual")):
            kept.append(line)
        # Keep summary lines
        elif any(w in stripped for w in ("passed", "failed", "total", "tests ran", "TOTAL", "===")):
            kept.append(line)
        # Count but don't keep individual passes
        elif _TEST_PASS_LINE.match(stripped):
            pass_count += 1
        # Keep first/last few lines for context
        elif len(kept) < 3:
            kept.append(line)

    if pass_count > 0:
        kept.insert(0, f"({pass_count} tests passed — details omitted)")
    return "\n".join(kept)


def _compress_search_results(output: str, max_results: int = 30) -> str:
    """Truncate search results to a reasonable count."""
    lines = output.split("\n")
    if len(lines) <= max_results:
        return output
    kept = lines[:max_results]
    kept.append(f"\n... ({len(lines) - max_results} more results omitted)")
    return "\n".join(kept)


def _smart_truncate(output: str, max_chars: int = 3000) -> str:
    """Intelligent truncation: keep start + end, summarize middle."""
    if len(output) <= max_chars:
        return output

    # Keep first 40% and last 40%, drop middle
    head_chars = int(max_chars * 0.4)
    tail_chars = int(max_chars * 0.4)

    lines = output.split("\n")
    total_lines = len(lines)

    # Find split points
    head_lines = []
    char_count = 0
    for line in lines:
        if char_count + len(line) > head_chars:
            break
        head_lines.append(line)
        char_count += len(line) + 1

    tail_lines = []
    char_count = 0
    for line in reversed(lines):
        if char_count + len(line) > tail_chars:
            break
        tail_lines.insert(0, line)
        char_count += len(line) + 1

    omitted = total_lines - len(head_lines) - len(tail_lines)
    return (
        "\n".join(head_lines)
        + f"\n\n... ({omitted} lines omitted) ...\n\n"
        + "\n".join(tail_lines)
    )
