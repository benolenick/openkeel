"""LocalEdit engine — delegate file edits to the best available local LLM.

Auto-detects GPU tier and routes to the best available model:
  Tier 1 (≤8B):  simple edits only (value changes, renames)
  Tier 2 (12-27B): complex edits (multi-line, add functions, refactors)
  Tier 3 (>30B):  full code delegation

Reads a file, sends it with a plain-English edit instruction to Ollama,
parses the JSON response (old_string / new_string), and applies the edit.
Creates a backup before modifying. Returns a short diff summary.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
from typing import Any

TIMEOUT = int(os.environ.get("LOCAL_EDIT_TIMEOUT", "60"))


def _get_endpoint(complex: bool = False) -> tuple[str, str, int]:
    """Get the best Ollama endpoint and model.

    complex=False (default): prefer the FAST endpoint — small model on a 3090.
        Used for simple/mechanical edits where ~200 tok/s matters more than raw
        capability. This is the 99% case.
    complex=True: escalate to the POWER endpoint (biggest available model).
        Used when the edit instruction is long, multi-step, or semantically tricky.
    """
    try:
        from openkeel.token_saver.engines.gpu_tier import (
            get_best_endpoint, get_fast_endpoint,
        )
        if not complex:
            fast = get_fast_endpoint()
            if fast is not None:
                return fast
        return get_best_endpoint()
    except Exception:
        return "http://127.0.0.1:11434", "gemma4:e2b", 1


def _classify_complexity(instruction: str) -> bool:
    """Return True if the instruction looks complex enough to need the big model.

    Heuristics:
      - more than 200 chars
      - mentions "refactor", "redesign", "rewrite", "architecture"
      - contains 3+ distinct verbs (very rough — counts common edit verbs)
    """
    if len(instruction) > 200:
        return True
    low = instruction.lower()
    if any(k in low for k in ("refactor", "redesign", "rewrite", "architecture",
                              "restructure", "multiple files")):
        return True
    verb_hits = sum(1 for v in ("add", "remove", "rename", "change", "replace",
                                "update", "move", "delete", "extract", "inline")
                    if v in low.split())
    return verb_hits >= 3

_SYSTEM_PROMPT = """\
You are a JSON code-edit machine. You receive a file snippet and an edit instruction.
You MUST respond with ONLY a JSON object. Nothing else. No code. No explanation.

Format: {"old_string": "exact text from file", "new_string": "replacement text"}

Example 1:
Instruction: Change timeout from 30 to 60
File has: TIMEOUT = 30
Response: {"old_string": "TIMEOUT = 30", "new_string": "TIMEOUT = 60"}

Example 2:
Instruction: Add import os after import sys
File has: import sys\\nimport json
Response: {"old_string": "import sys\\nimport json", "new_string": "import sys\\nimport os\\nimport json"}

Example 3:
Instruction: Remove the TODO comment
File has: # TODO: fix this\\ndef main():
Response: {"old_string": "# TODO: fix this\\n", "new_string": ""}

CRITICAL: old_string must be COPIED EXACTLY from the file. Keep it SHORT (1-3 lines max).\
"""


def _ollama_chat(system: str, user: str, max_tokens: int = 1024,
                 ollama_url: str = "", model: str = "") -> str:
    """Send a chat request to Ollama /api/chat. Returns empty string on failure."""
    if not ollama_url or not model:
        ollama_url, model, _ = _get_endpoint()

    # Use /api/generate — more reliable think:false support across Ollama versions
    full_prompt = f"{system}\n\n{user}"
    payload = json.dumps({
        "model": model,
        "prompt": full_prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.0,
            "num_predict": max_tokens,
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{ollama_url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("response", "")
    except Exception as e:
        return ""


def _extract_json(text: str) -> dict | None:
    """Try to parse a JSON object from possibly noisy LLM output.

    Handles: markdown fences, surrounding text, broken escaping from small models.
    """
    text = text.strip()
    # Strip markdown fences
    if "```" in text:
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find first { ... last }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        # Small models often produce broken JSON with unescaped newlines in strings.
        # Try to fix by extracting old_string and new_string values manually.
        try:
            import re
            old_match = re.search(r'"old_string"\s*:\s*"((?:[^"\\]|\\.)*)"', candidate, re.DOTALL)
            new_match = re.search(r'"new_string"\s*:\s*"((?:[^"\\]|\\.)*)"', candidate, re.DOTALL)
            if old_match and new_match:
                old_val = old_match.group(1).replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
                new_val = new_match.group(1).replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
                return {"old_string": old_val, "new_string": new_val}
        except Exception:
            pass

        # Last resort: try fixing common JSON issues
        try:
            # Replace literal newlines inside strings with \n
            fixed = re.sub(r'(?<=": ")(.*?)(?="[,}])', lambda m: m.group(0).replace("\n", "\\n"), candidate, flags=re.DOTALL)
            return json.loads(fixed)
        except Exception:
            pass

    return None


def _build_context(content: str, lines: list[str], instruction: str, max_lines: int = 80) -> str:
    """For large files, send only the relevant portion around the likely edit target.

    Searches for keywords from the instruction to find the target area,
    then sends ±max_lines/2 lines around it. This keeps gemma4 focused
    and prevents it from trying to rewrite the entire file.
    """
    if len(lines) <= max_lines:
        return content

    # Extract keywords from instruction to find target area
    import re
    # Look for quoted strings, identifiers, and significant words
    keywords = re.findall(r'"([^"]+)"|\'([^\']+)\'|(\b[A-Z_]{2,}\b)|(\b\w+(?:_\w+)+\b)', instruction)
    search_terms = [k for group in keywords for k in group if k]

    # Also try individual significant words (>3 chars, not common words)
    skip = {"from", "with", "that", "this", "after", "before", "change", "replace", "remove", "delete", "add", "the", "line"}
    words = [w for w in instruction.split() if len(w) > 3 and w.lower() not in skip]
    search_terms.extend(words[:5])

    # Find best matching line
    best_line = len(lines) // 2  # default to middle
    best_score = 0
    for i, line in enumerate(lines):
        score = sum(1 for term in search_terms if term.lower() in line.lower())
        if score > best_score:
            best_score = score
            best_line = i

    # Extract window around target
    half = max_lines // 2
    start = max(0, best_line - half)
    end = min(len(lines), best_line + half)

    snippet_lines = []
    if start > 0:
        snippet_lines.append(f"... (lines 1-{start} omitted) ...")
    for i in range(start, end):
        # Do NOT include line numbers — gemma4 copies them into old_string
        snippet_lines.append(lines[i])
    if end < len(lines):
        snippet_lines.append(f"... (lines {end+1}-{len(lines)} omitted) ...")

    return "\n".join(snippet_lines)


def _make_diff(lines: list[str], old_string: str, new_string: str) -> str:
    """Build a short diff summary showing context around the change."""
    # Find the line range of old_string in the original content
    content = "\n".join(lines)
    pos = content.find(old_string)
    if pos < 0:
        return "(change applied but diff unavailable)"

    # Count lines before the match
    prefix = content[:pos]
    start_line = prefix.count("\n")
    old_line_count = old_string.count("\n") + 1
    new_lines_list = new_string.split("\n")

    ctx = 3  # lines of context
    begin = max(0, start_line - ctx)
    end = min(len(lines), start_line + old_line_count + ctx)

    diff_parts = []
    diff_parts.append(f"--- {begin + 1},{end} ---")
    for i in range(begin, min(start_line, end)):
        diff_parts.append(f"  {i + 1}\t{lines[i]}")

    for line in old_string.split("\n"):
        diff_parts.append(f"- {start_line + 1}\t{line}")
    for line in new_lines_list:
        diff_parts.append(f"+ \t{line}")

    after_start = start_line + old_line_count
    for i in range(after_start, end):
        if i < len(lines):
            diff_parts.append(f"  {i + 1}\t{lines[i]}")

    return "\n".join(diff_parts)


def apply_edit(file_path: str, instruction: str) -> dict[str, Any]:
    """Main entry point. Returns a result dict with status, diff, and error info.

    Result keys:
        success (bool): Whether the edit was applied.
        diff (str): Short diff summary if successful.
        error (str): Error message if failed.
        old_string (str): What was replaced (for logging).
        new_string (str): What it was replaced with (for logging).
        file_path (str): The edited file.
        backup_path (str): Path to the .localedit.bak file.
    """
    # Detect best available model — escalate to power endpoint only if the
    # instruction looks complex. Default: fast 3B on jagg 3090 (~200 tok/s).
    is_complex = _classify_complexity(instruction)
    ollama_url, model_name, tier = _get_endpoint(complex=is_complex)

    result: dict[str, Any] = {
        "success": False,
        "diff": "",
        "error": "",
        "old_string": "",
        "new_string": "",
        "file_path": file_path,
        "backup_path": "",
        "model": model_name,
        "tier": tier,
    }

    if not ollama_url:
        result["error"] = "No Ollama endpoint available (tier 0)"
        return result

    # --- Read the file ---
    if not os.path.isfile(file_path):
        result["error"] = f"File not found: {file_path}"
        return result

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        result["error"] = f"Cannot read file: {e}"
        return result

    lines = content.split("\n")
    line_count = len(lines)

    # --- Build the prompt, trimming large files ---
    # Tier 2+ models get more context (200 lines vs 80)
    max_ctx = 200 if tier >= 2 else 80
    send_content = _build_context(content, lines, instruction, max_lines=max_ctx)
    trimmed = len(lines) > max_ctx

    user_prompt = (
        f"File: {os.path.basename(file_path)} ({line_count} lines)"
        f"{' [showing relevant section]' if trimmed else ''}\n"
        f"Instruction: {instruction}\n\n"
        f"{send_content}\n\n"
        f"Respond with ONLY the JSON object. No code blocks."
    )

    # --- Ask LLM (retry once on empty response) ---

    raw_response = _ollama_chat(_SYSTEM_PROMPT, user_prompt, ollama_url=ollama_url, model=model_name)
    if not raw_response:
        raw_response = _ollama_chat(_SYSTEM_PROMPT, user_prompt, ollama_url=ollama_url, model=model_name)
    if not raw_response:
        result["error"] = f"Ollama ({model_name}@{ollama_url}) returned empty after retry"
        return result

    # --- Parse response ---
    parsed = _extract_json(raw_response)
    if not parsed:
        result["error"] = f"Could not parse JSON from LLM response: {raw_response[:200]}"
        return result

    old_string = parsed.get("old_string", "")
    new_string = parsed.get("new_string", "")

    if not old_string:
        result["error"] = "LLM returned empty old_string"
        return result

    result["old_string"] = old_string
    result["new_string"] = new_string

    # --- Validate: old_string must exist exactly once ---
    count = content.count(old_string)
    if count == 0:
        result["error"] = f"old_string not found in file. LLM returned: {old_string[:120]}"
        return result
    if count > 1:
        result["error"] = f"old_string found {count} times (ambiguous). LLM returned: {old_string[:120]}"
        return result

    # --- Create backup ---
    backup_path = file_path + ".localedit.bak"
    try:
        shutil.copy2(file_path, backup_path)
        result["backup_path"] = backup_path
    except Exception as e:
        result["error"] = f"Cannot create backup: {e}"
        return result

    # --- Apply edit ---
    new_content = content.replace(old_string, new_string, 1)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        # Restore from backup
        try:
            shutil.copy2(backup_path, file_path)
        except Exception:
            pass
        result["error"] = f"Cannot write file: {e}"
        return result

    # --- v5 post-write verification: real diff, AST parse, auto-rollback ---
    # Replaces the old fake "X lines changed" summary (which came from the
    # LLM's self-report, not a real diff). If the edit breaks .py syntax,
    # v5's verify_edit restores from backup automatically.
    try:
        from openkeel.token_saver_v5 import localedit_verify
        from openkeel.token_saver_v5 import debug_log as _v5_log
        ver = localedit_verify.verify_edit(
            file_path, content, new_content,
            backup_path=backup_path, require_py_valid=True,
        )
        if not ver.ok:
            _v5_log.note(
                "local_edit.verify_edit",
                f"rejected: {ver.reason}",
                tool="Bash", file_path=file_path,
                rolled_back=ver.rolled_back,
            )
            result["error"] = (
                f"v5 verify rejected edit: {ver.reason}"
                + (" (rolled back from backup)" if ver.rolled_back else "")
            )
            return result
        result["diff"] = ver.diff[:2000] if ver.diff else _make_diff(lines, old_string, new_string)
        result["lines_changed"] = ver.lines_changed
        result["added"] = ver.added
        result["removed"] = ver.removed
        result["success"] = True
        return result
    except Exception as e:
        # v5 not importable — fall through to legacy diff. This preserves
        # backward compatibility if v5 is ever removed.
        try:
            from openkeel.token_saver_v5 import debug_log as _v5_log
            _v5_log.swallow("local_edit.verify_import", error=e, tool="Bash")
        except Exception:
            pass

    # --- Legacy diff fallback ---
    result["diff"] = _make_diff(lines, old_string, new_string)
    result["success"] = True
    return result
