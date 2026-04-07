#!/usr/bin/env python3
"""PreToolUse hook for token saver — active interception layer.

Intercepts tool calls BEFORE execution to save tokens via:
  1. File re-read caching (serve summaries instead of full files)
  2. Bash command rewriting (run compact version, serve compressed output)
  3. Bash output compression (execute + compress verbose commands)

Protocol: reads JSON from stdin, outputs JSON to stdout.
  - {"decision": "block", "reason": "..."} to block and replace
  - {"decision": "allow"} or no output to allow
  - Must complete within 2 seconds (5s for bash interceptions)

Fail-open: any error → allow the tool call through.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

DAEMON_URL = os.environ.get("TOKEN_SAVER_DAEMON", "http://127.0.0.1:11450")
EDITED_FILES_PATH = os.path.expanduser("~/.openkeel/scribe_state.json")

# Minimum output size (chars) before we bother compressing
_MIN_COMPRESS = 800   # was 1500 — lowered 2026-04-07 for more aggressive LLM summarization
_MIN_LLM_SUMMARIZE = 1200  # was 2000 — now that hot path is qwen2.5:3b @ 200 tok/s
# Max chars to return after compression
_MAX_OUTPUT = 3000

# Persist session state across hook invocations via temp file
_STATE_FILE = os.path.join(tempfile.gettempdir(), f"token_saver_session_{os.getppid()}.json")

def _load_session_state() -> dict:
    try:
        with open(_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"mtimes": {}, "read_files": []}

def _save_session_state(state: dict) -> None:
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def _daemon_get(path: str) -> dict | None:
    try:
        req = urllib.request.Request(f"{DAEMON_URL}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=1) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _daemon_post(path: str, data: dict) -> dict | None:
    try:
        payload = json.dumps(data).encode()
        req = urllib.request.Request(
            f"{DAEMON_URL}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _get_edited_files() -> set:
    try:
        with open(EDITED_FILES_PATH, "r") as f:
            state = json.load(f)
        return set(state.get("files_edited", []) + state.get("files_created", []))
    except Exception:
        return set()


def _check_session_read(file_path: str) -> bool:
    result = _daemon_post("/session/read", {"path": file_path})
    if result:
        return result.get("already_read", False)
    return False


def _record_savings(event_type: str, tool_name: str, original_chars: int,
                    saved_chars: int, notes: str = "", file_path: str = "") -> None:
    _daemon_post("/ledger/record", {
        "event_type": event_type,
        "tool_name": tool_name,
        "file_path": file_path,
        "original_chars": original_chars,
        "saved_chars": saved_chars,
        "notes": notes,
    })


def _run_cmd(command: str, timeout: int = 3) -> tuple[str, int]:
    """Execute a shell command, return (output, return_code)."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout,
        )
        output = result.stdout
        if result.stderr:
            output += ("\n" + result.stderr) if output else result.stderr
        return output.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "(command timed out)", 1
    except Exception as e:
        return str(e), 1


# ANSI strip
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _try_compress_json(raw: str) -> str | None:
    """Compress JSON API responses (Hyphae recall, Kanban, etc.).

    Hyphae /recall returns {"results": [{"text": "...", "score": 0.5, ...}, ...]}.
    These are often 10-30k chars. We apply:
      - Score-based filtering (drop < 0.3)
      - Score-based truncation (low scores get shorter text)
      - Deduplication (skip results whose text is a substring of a kept result)
      - Briefing preference (if a briefing exists, skip individual facts it covers)
      - Adaptive limit (stop when cumulative text > 2500 chars)
    """
    try:
        data = json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    # Hyphae recall response
    if "results" in data and isinstance(data["results"], list):
        results = data["results"]

        # Sort by score descending
        results = sorted(results, key=lambda r: r.get("score", 0), reverse=True)

        # Filter by minimum score
        results = [r for r in results if r.get("score", 0) >= 0.3]

        # Prefer briefings — if one exists, it summarizes other facts
        briefing = None
        non_briefing = []
        for r in results:
            src = r.get("source", "")
            if "briefing" in src or "distill" in src:
                if briefing is None:
                    briefing = r
            else:
                non_briefing.append(r)

        # If we have a briefing, use it + top 3 non-briefing results
        if briefing:
            results = [briefing] + non_briefing[:3]
        else:
            results = results[:8]

        # Deduplicate — skip results whose text is largely contained in a kept result
        kept_texts = []
        deduped = []
        for r in results:
            text = r.get("text", "")
            # Check if this is a near-duplicate of something we already kept
            is_dup = False
            text_lower = text.lower()[:200]
            for kept in kept_texts:
                # If 60%+ of the first 200 chars match, it's a duplicate
                overlap = sum(1 for w in text_lower.split() if w in kept)
                if overlap > len(text_lower.split()) * 0.6:
                    is_dup = True
                    break
            if not is_dup:
                deduped.append(r)
                kept_texts.append(text_lower)

        # Build compressed results with score-based truncation
        compressed_results = []
        cumulative = 0
        for r in deduped:
            score = r.get("score", 0)
            text = r.get("text", "")

            # Score-based text budget
            if score >= 0.6:
                max_len = 400
            elif score >= 0.45:
                max_len = 250
            else:
                max_len = 150

            if len(text) > max_len:
                text = text[:max_len] + "..."

            cumulative += len(text)
            compressed_results.append({
                "text": text,
                "score": round(score, 3),
                "source": r.get("source", ""),
            })

            # Adaptive limit — stop if we've accumulated enough context
            if cumulative > 2500:
                break

        return json.dumps({"results": compressed_results}, indent=1)

    # Kanban board response (list of tasks)
    if isinstance(data, list) and data and isinstance(data[0], dict) and "title" in data[0]:
        compressed = []
        for item in data[:20]:
            compressed.append({
                "id": item.get("id"),
                "title": item.get("title", "")[:100],
                "status": item.get("status"),
                "priority": item.get("priority"),
            })
        return json.dumps(compressed, indent=1)

    # Generic large JSON — compact it
    compact = json.dumps(data, separators=(",", ":"))
    if len(compact) < len(raw) * 0.7:
        return compact

    return None


def _run_and_compress(command: str, timeout: int = 15, label: str = "",
                      json_compress: bool = False) -> dict | None:
    """Run a command and compress if output is large. Returns block dict or None."""
    output, rc = _run_cmd(command + " 2>&1" if "2>&1" not in command else command, timeout=timeout)
    raw = _strip_ansi(output)

    if len(raw) <= _MIN_COMPRESS:
        # Small output — let Claude handle it normally (don't block)
        return None

    # Try JSON compression if flagged
    compressed = None
    if json_compress:
        compressed = _try_compress_json(raw)

    if compressed is None:
        compressed = _smart_truncate(raw, _MAX_OUTPUT)

    saved = max(0, len(raw) - len(compressed))
    if saved > 200:
        # LLM summarization — aggressive mode. Fires on piped commands too now
        # that the hot path is qwen2.5:3b @ ~200 tok/s on jagg's 3090.
        if len(raw) > _MIN_LLM_SUMMARIZE:
            try:
                from openkeel.token_saver.engines.llm_calibrator import should_use_llm
                if not should_use_llm("summarization"):
                    raise RuntimeError("LLM not trusted for summarization")
                from openkeel.token_saver.summarizer import summarize_bash_output
                raw_lines = len(raw.split("\n"))
                min_lines = max(15, int(raw_lines * 0.2))  # at least 15 lines or 20%
                llm_result = summarize_bash_output(command, raw, max_lines=min_lines)
                if llm_result and len(llm_result) < len(compressed):
                    compressed = (
                        f"[TOKEN SAVER] Output summarized by LLM ({len(raw)} → {len(llm_result)} chars).\n"
                        f"Run the command again if you need raw output.\n\n{llm_result}"
                    )
                    saved = max(0, len(raw) - len(compressed))
                    _record_savings("bash_llm_summarize", "Bash", len(raw), saved,
                                    f"{label}: LLM summarized {command[:60]}")
                    return {"decision": "block", "reason": compressed}
            except Exception:
                pass

        _record_savings("bash_compress", "Bash", len(raw), saved,
                        f"{label}: {command[:80]}")
        return {"decision": "block", "reason": compressed}

    # No meaningful savings — let the original command run
    return None


def _smart_truncate(output: str, max_chars: int = _MAX_OUTPUT) -> str:
    """Keep head + tail, drop middle."""
    if len(output) <= max_chars:
        return output
    head = int(max_chars * 0.45)
    tail = int(max_chars * 0.45)
    lines = output.split("\n")
    head_lines, tail_lines = [], []
    chars = 0
    for line in lines:
        if chars + len(line) > head:
            break
        head_lines.append(line)
        chars += len(line) + 1
    chars = 0
    for line in reversed(lines):
        if chars + len(line) > tail:
            break
        tail_lines.insert(0, line)
        chars += len(line) + 1
    omitted = len(lines) - len(head_lines) - len(tail_lines)
    if omitted <= 0:
        return output
    return "\n".join(head_lines) + f"\n\n... ({omitted} lines omitted) ...\n\n" + "\n".join(tail_lines)


# ---------------------------------------------------------------------------
# Engine: File Re-read Caching
# ---------------------------------------------------------------------------

def handle_read(tool_input: dict) -> dict | None:
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return None

    # Never cache files being edited
    if file_path in _get_edited_files():
        return None

    try:
        stat = os.stat(file_path)
    except OSError:
        return None

    # Don't intercept small files
    if stat.st_size < 4000:
        _check_session_read(file_path)
        return None

    # If specific lines requested, let it through
    if tool_input.get("offset") or tool_input.get("limit"):
        _check_session_read(file_path)
        state = _load_session_state()
        state.setdefault("read_files", [])
        if file_path not in state["read_files"]:
            state["read_files"].append(file_path)
        state.setdefault("mtimes", {})[file_path] = stat.st_mtime
        _save_session_state(state)
        return None

    # If this is a re-read, try to serve a summary
    already_read = _check_session_read(file_path)
    state = _load_session_state()
    state.setdefault("read_files", [])
    if file_path not in state["read_files"]:
        state["read_files"].append(file_path)

    if already_read:
        # Check if file changed since last read via mtime
        last_mtime = state.get("mtimes", {}).get(file_path)
        current_mtime = stat.st_mtime

        if last_mtime and current_mtime == last_mtime:
            # File hasn't changed — give a very short "unchanged" response
            orig_chars = stat.st_size
            short_msg = (
                f"[TOKEN SAVER] File unchanged since last read ({stat.st_size} bytes, "
                f"mtime unchanged). Use Read with offset/limit for specific sections.\n"
                f"File: {file_path}"
            )
            _record_savings(
                "reread_unchanged", "Read", orig_chars,
                max(0, orig_chars - len(short_msg)),
                f"re-read unchanged: {file_path}",
                file_path,
            )
            return {"decision": "block", "reason": short_msg}

        # File may have changed or first re-read — try LLM-enriched summary
        state["mtimes"] = state.get("mtimes", {})
        state["mtimes"][file_path] = current_mtime
        _save_session_state(state)

        # Try daemon summary first (fast path)
        result = _daemon_post("/summarize", {"path": file_path})
        if result and result.get("summary"):
            summary = result["summary"]
            orig_lines = result.get("original_lines", 0)
            orig_chars = result.get("original_chars", 0)

            # Try to get a richer LLM re-read summary if file is large
            if orig_chars > 8000:
                try:
                    from openkeel.token_saver.summarizer import summarize_file_reread
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    llm_summary = summarize_file_reread(content, file_path)
                    if llm_summary and len(llm_summary) > len(summary):
                        summary = llm_summary
                except Exception:
                    pass

            summary_chars = len(summary)
            _record_savings(
                "cache_hit", "Read", orig_chars,
                max(0, orig_chars - summary_chars),
                f"re-read: {len(summary.splitlines())}L summary vs {orig_lines}L original",
                file_path,
            )

            return {
                "decision": "block",
                "reason": (
                    f"[TOKEN SAVER] You already read this file. Here's a summary "
                    f"({len(summary.splitlines())} lines vs {orig_lines} original). "
                    f"Use Read with specific offset/limit if you need exact content.\n\n"
                    f"File: {file_path}\n{summary}"
                ),
            }

    # --- First-read large file compression ---
    # For large files (>8KB), provide head + structure + tail instead of full content
    _LARGE_FILE_THRESHOLD = 8000
    if stat.st_size > _LARGE_FILE_THRESHOLD and not already_read:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            return None

        if len(content) < _LARGE_FILE_THRESHOLD:
            return None  # Size on disk != text size, recheck

        lines = content.split("\n")
        total_lines = len(lines)

        # Build structural summary: classes, functions, imports
        structure = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(("class ", "def ", "async def ", "export ", "function ")):
                structure.append(f"  L{i+1}: {stripped[:120]}")
            elif stripped.startswith(("import ", "from ")) and i < 30:
                structure.append(f"  L{i+1}: {stripped[:120]}")
            if len(structure) >= 40:
                break

        # Head (first 80 lines) + structure + tail (last 30 lines)
        head = "\n".join(f"{i+1}\t{l}" for i, l in enumerate(lines[:80]))
        tail = "\n".join(f"{total_lines - len(lines[-30:]) + i + 1}\t{l}"
                         for i, l in enumerate(lines[-30:]))
        struct_block = "\n".join(structure) if structure else "  (no top-level definitions found)"

        compressed = (
            f"[TOKEN SAVER] Large file ({total_lines} lines, {len(content)} chars). "
            f"Showing head + structure + tail. Use Read with offset/limit for specific sections.\n\n"
            f"File: {file_path}\n\n"
            f"=== FIRST 80 LINES ===\n{head}\n\n"
            f"=== STRUCTURE (classes/functions/exports) ===\n{struct_block}\n\n"
            f"=== LAST 30 LINES ===\n{tail}"
        )

        # v4: try LLM-generated semantic skeleton as a tighter alternative
        if os.environ.get("TOKEN_SAVER_V4") == "1" and len(content) > 4000:
            try:
                from openkeel.token_saver_v4.engines import llm_engines as _v4e
                skel = _v4e.semantic_skeleton(content, file_path)
                if skel and len(skel) < len(compressed):
                    v4_block = (
                        f"[TOKEN SAVER v4] Semantic skeleton of {total_lines}-line file "
                        f"(LLM-generated, ~{100 - int(100*len(skel)/len(content))}% smaller than raw). "
                        f"Use Read with offset/limit for specific sections.\n\n"
                        f"File: {file_path}\n\n{skel}"
                    )
                    if len(v4_block) < len(compressed):
                        _record_savings(
                            "v4_semantic_skeleton", "Read",
                            len(content), max(0, len(content) - len(v4_block)),
                            f"first-read skeleton: {total_lines}L → LLM skeleton",
                            file_path,
                        )
                        return {"decision": "block", "reason": v4_block}
            except Exception:
                pass  # fall through to head+structure+tail

        saved = max(0, len(content) - len(compressed))
        if saved > 2000:
            _record_savings(
                "large_file_compress", "Read", len(content), saved,
                f"first-read large: {total_lines}L → head+structure+tail",
                file_path,
            )
            return {"decision": "block", "reason": compressed}

    return None


# ---------------------------------------------------------------------------
# Engine: Bash Command Interception
# ---------------------------------------------------------------------------

# Patterns for commands we can safely run-and-compress
_GIT_VERBOSE_RE = re.compile(r"^(remote: (Counting|Compressing|Total|Enumerating))")
_PKG_NOISE_WORDS = {"npm warn", "added", "up to date", "audited", "Requirement already",
                     "Downloading", "Using cached", "Installing collected"}
_TEST_PASS_RE = re.compile(r"^.*PASS(ED)?.*$", re.MULTILINE)


def _compress_pkg_output(output: str) -> str:
    """Compress npm/pip install output to errors + summary."""
    lines = output.split("\n")
    kept = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if any(w in s.lower() for w in ("error", "err!", "warn", "vulnerability", "deprecated")):
            kept.append(line)
        if any(w in s for w in ("added", "up to date", "Successfully installed", "audited")):
            kept.append(line)
    return "\n".join(kept) if kept else "(install completed, no errors)"


def _compress_git_remote(output: str) -> str:
    """Compress git push/pull/fetch — remove progress noise."""
    lines = output.split("\n")
    kept = [l for l in lines if l.strip() and not _GIT_VERBOSE_RE.match(l.strip())]
    return "\n".join(kept) if kept else "(completed)"


def _compress_test_output(output: str) -> str:
    """Compress test output — keep failures + summary, skip individual passes."""
    lines = output.split("\n")
    kept = []
    pass_count = 0
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if any(w in s.lower() for w in ("fail", "error", "assert", "traceback", "expected", "actual")):
            kept.append(line)
        elif any(w in s for w in ("passed", "failed", "total", "tests ran", "TOTAL", "===")):
            kept.append(line)
        elif _TEST_PASS_RE.match(s):
            pass_count += 1
        elif len(kept) < 3:
            kept.append(line)
    if pass_count > 0:
        kept.insert(0, f"({pass_count} tests passed — details omitted)")
    return "\n".join(kept) if kept else output


def _compress_build_output(output: str) -> str:
    """Compress build output to errors + final status."""
    lines = output.split("\n")
    kept = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if any(w in s.lower() for w in ("error", "warning", "failed", "cannot find")):
            kept.append(line)
        if any(w in s.lower() for w in ("finished", "built", "compiled", "success")):
            kept.append(line)
    if not kept:
        kept = [l for l in lines[-5:] if l.strip()]
    return "\n".join(kept)


def handle_local_edit(command: str) -> dict | None:
    """Handle #LOCALEDIT: convention — delegate simple edits to local LLM.

    Format: #LOCALEDIT: /path/to/file.py | Edit instruction here
    """
    try:
        # Parse: everything after "#LOCALEDIT:" is "path | instruction"
        payload = command.split("#LOCALEDIT:", 1)[1].strip()
        if "|" not in payload:
            return {"decision": "block", "reason": "[LOCAL EDIT] Error: format must be '#LOCALEDIT: /path | instruction'"}

        file_path, instruction = payload.split("|", 1)
        file_path = file_path.strip()
        instruction = instruction.strip()

        if not file_path or not instruction:
            return {"decision": "block", "reason": "[LOCAL EDIT] Error: empty file path or instruction"}

        from openkeel.token_saver.engines.local_edit import apply_edit

        result = apply_edit(file_path, instruction)

        # Estimate savings: Edit tool would send full file content back (~file size)
        # plus the old/new strings. Local edit uses only a short diff confirmation.
        try:
            file_size = os.path.getsize(file_path)
        except OSError:
            file_size = 2000

        # Edit tool cost: file content echoed back + tool overhead (~file_size chars)
        # LocalEdit cost: just the diff summary (~200-500 chars)
        edit_tool_cost = file_size
        local_cost = len(result.get("diff", "")) + len(result.get("error", ""))
        saved = max(0, edit_tool_cost - local_cost)

        if result["success"]:
            _record_savings(
                "local_edit", "Bash", edit_tool_cost, saved,
                f"local_edit OK: {os.path.basename(file_path)} — {instruction[:60]}",
                file_path,
            )
            msg = (
                f"[LOCAL EDIT] Success — {os.path.basename(file_path)}\n"
                f"Backup: {result['backup_path']}\n\n"
                f"{result['diff']}"
            )
            return {"decision": "block", "reason": msg}
        else:
            _record_savings(
                "local_edit_fail", "Bash", edit_tool_cost, 0,
                f"local_edit FAIL: {os.path.basename(file_path)} — {result['error'][:80]}",
                file_path,
            )
            msg = (
                f"[LOCAL EDIT] Failed — {os.path.basename(file_path)}\n"
                f"Error: {result['error']}\n"
                f"Instruction was: {instruction}\n"
                f"You may need to use the Edit tool directly."
            )
            return {"decision": "block", "reason": msg}

    except Exception as e:
        # Fail-open: if anything goes wrong, record and let the command through
        _record_savings("local_edit_fail", "Bash", 0, 0, f"local_edit exception: {e}")
        return {"decision": "block", "reason": f"[LOCAL EDIT] Internal error: {e}"}


def handle_bash(tool_input: dict) -> dict | None:
    command = tool_input.get("command", "").strip()
    if not command:
        return None

    # --- LocalEdit convention: #LOCALEDIT: /path | instruction ---
    if command.startswith("#LOCALEDIT:"):
        return handle_local_edit(command)

    # Skip heredocs — too risky to modify
    if "<<" in command:
        return None

    cmd_base = command.split("|")[0].strip().split("&&")[0].strip()
    cmd_lower = cmd_base.lower()

    # --- Package managers: run with quiet flags, compress output ---
    if any(cmd_lower.startswith(p) for p in ("npm install", "npm ci", "yarn install", "yarn add")):
        output, rc = _run_cmd(command + " --silent 2>&1", timeout=120)
        compressed = _compress_pkg_output(_strip_ansi(output))
        orig_len = len(output)
        saved = max(0, orig_len - len(compressed))
        if saved > 100:
            _record_savings("bash_compress", "Bash", orig_len, saved,
                            f"npm: {len(compressed)} chars from {orig_len}")
        return {"decision": "block", "reason": f"[TOKEN SAVER] Ran with --silent:\n{compressed}"}

    if any(cmd_lower.startswith(p) for p in ("pip install", "pip3 install")):
        output, rc = _run_cmd(command + " -q 2>&1", timeout=120)
        compressed = _compress_pkg_output(_strip_ansi(output))
        orig_len = len(output)
        saved = max(0, orig_len - len(compressed))
        if saved > 100:
            _record_savings("bash_compress", "Bash", orig_len, saved,
                            f"pip: {len(compressed)} chars from {orig_len}")
        return {"decision": "block", "reason": f"[TOKEN SAVER] Ran with -q:\n{compressed}"}

    # --- Git operations: run + compress ---
    if any(cmd_lower.startswith(p) for p in ("git push", "git pull", "git fetch", "git clone")):
        output, rc = _run_cmd(command + " 2>&1", timeout=30)
        compressed = _compress_git_remote(_strip_ansi(output))
        orig_len = len(output)
        saved = max(0, orig_len - len(compressed))
        if saved > 100:
            _record_savings("bash_compress", "Bash", orig_len, saved,
                            f"git: {len(compressed)} chars from {orig_len}")
            return {"decision": "block", "reason": f"[TOKEN SAVER] Compressed git output:\n{compressed}"}
        # If no savings, return the output as-is (still blocked since we already ran it)
        return {"decision": "block", "reason": compressed}

    # --- Git log without limits ---
    if cmd_lower.startswith("git log") and " -n" not in command and "--oneline" not in command and "| head" not in command:
        if len(command.split()) <= 3:
            output, rc = _run_cmd("git log --oneline -20", timeout=5)
            _record_savings("bash_compress", "Bash", 2000, 1500,
                            "git log → --oneline -20")
            return {"decision": "block", "reason": f"[TOKEN SAVER] Ran `git log --oneline -20`:\n{output}"}

    # --- Test commands: run + compress ---
    if any(k in cmd_lower for k in ("pytest", "jest ", "mocha ", "cargo test", "go test", "npm test", "npm run test")):
        output, rc = _run_cmd(command + " 2>&1", timeout=120)
        raw = _strip_ansi(output)
        if len(raw) > _MIN_COMPRESS:
            compressed = _compress_test_output(raw)
            saved = max(0, len(raw) - len(compressed))
            if saved > 200:
                _record_savings("bash_compress", "Bash", len(raw), saved,
                                f"test: {len(compressed)} chars from {len(raw)}")
                return {
                    "decision": "block",
                    "reason": f"[TOKEN SAVER] Compressed test output ({len(compressed)} chars from {len(raw)}):\n{compressed}",
                }
        # Small output or no savings — still return since we already ran it
        return {"decision": "block", "reason": raw if raw else "(no output)"}

    # --- Build commands: run + compress ---
    if any(cmd_lower.startswith(p) for p in ("cargo build", "go build", "make ", "make\n", "gcc ", "g++ ")):
        output, rc = _run_cmd(command + " 2>&1", timeout=120)
        raw = _strip_ansi(output)
        if len(raw) > _MIN_COMPRESS:
            compressed = _compress_build_output(raw)
            saved = max(0, len(raw) - len(compressed))
            if saved > 200:
                _record_savings("bash_compress", "Bash", len(raw), saved,
                                f"build: {len(compressed)} chars from {len(raw)}")
                return {"decision": "block", "reason": f"[TOKEN SAVER] Compressed build output:\n{compressed}"}
        return {"decision": "block", "reason": raw if raw else "(no output)"}

    # --- Docker build: run with quiet flag ---
    if cmd_lower.startswith("docker build"):
        output, rc = _run_cmd(command + " -q 2>&1", timeout=300)
        compressed = _strip_ansi(output)
        if len(compressed) > _MAX_OUTPUT:
            compressed = _smart_truncate(compressed)
        _record_savings("bash_compress", "Bash", len(output), max(0, len(output) - len(compressed)),
                        "docker build -q")
        return {"decision": "block", "reason": f"[TOKEN SAVER] Ran with -q:\n{compressed}"}

    # --- Directory listings: run + truncate if huge ---
    if any(cmd_lower.startswith(p) for p in ("find ", "tree ", "du ")):
        if "| head" not in command and "| tail" not in command and "-maxdepth" not in command:
            return _run_and_compress(command, timeout=10, label="dir listing")

    # --- Any curl command: run + compress (JSON-aware for API endpoints) ---
    if "curl" in cmd_lower:
        # Detect JSON API endpoints for smarter compression
        is_json_api = any(p in command for p in ("8100", "8200", "8101", "/api/", "/recall", "/health",
                                                  "application/json", "-H", "--header"))
        return _run_and_compress(command, timeout=15, label="curl", json_compress=is_json_api)

    # --- SSH commands (avg 7k chars, #2 consumer) ---
    if cmd_lower.startswith("ssh "):
        return _run_and_compress(command, timeout=30, label="ssh")

    # --- cat/tail/head of log files and large files ---
    if any(cmd_lower.startswith(p) for p in ("cat ", "tail ", "head ")):
        # Only intercept if reading something likely to be large (logs, .py, .json, etc.)
        if any(ext in command for ext in (".log", ".json", ".py", ".txt", ".csv", ".html", "/tmp/")):
            return _run_and_compress(command, timeout=5, label="file read")

    # --- journalctl, dmesg, syslog ---
    if any(cmd_lower.startswith(p) for p in ("journalctl", "dmesg", "sudo journalctl", "sudo dmesg")):
        return _run_and_compress(command, timeout=10, label="syslog")

    # --- ps aux, systemctl, process listings ---
    if any(cmd_lower.startswith(p) for p in ("ps aux", "ps -", "systemctl list", "systemctl --user list")) and "|" not in command:
        return _run_and_compress(command, timeout=5, label="process list")

    # --- Known-huge listing commands: just return counts ---
    if cmd_lower.startswith(("dpkg -l", "dpkg --list")) and "|" not in command:
        raw, rc = _run_cmd(command, timeout=10)
        if rc == 0 and raw:
            line_count = len(raw.strip().split("\n"))
            if line_count > 50:
                # Count packages, show first 10 + count
                lines = raw.strip().split("\n")
                header = [l for l in lines[:5] if l.startswith(("||", "++", "Desired"))]
                pkgs = [l for l in lines if l.startswith("ii ")]
                summary = (
                    "\n".join(header) + "\n"
                    f"\n{len(pkgs)} packages installed. Showing first 15:\n"
                    + "\n".join(pkgs[:15])
                    + f"\n... ({len(pkgs) - 15} more)"
                )
                saved = len(raw) - len(summary)
                if saved > 500:
                    _record_savings("bash_predict", "Bash", len(raw), saved,
                                    f"dpkg -l: {len(pkgs)} packages → summary", "")
                    return {"decision": "block", "reason": summary}

    if cmd_lower.startswith(("pip list", "pip3 list")) and "|" not in command:
        raw, rc = _run_cmd(command, timeout=10)
        if rc == 0 and raw:
            lines = raw.strip().split("\n")
            if len(lines) > 40:
                pkg_lines = [l for l in lines[2:] if l.strip()]  # skip header
                summary = (
                    f"{len(pkg_lines)} packages installed. Showing first 15:\n"
                    + "\n".join(lines[:2]) + "\n"
                    + "\n".join(pkg_lines[:15])
                    + f"\n... ({len(pkg_lines) - 15} more)"
                )
                saved = len(raw) - len(summary)
                if saved > 500:
                    _record_savings("bash_predict", "Bash", len(raw), saved,
                                    f"pip list: {len(pkg_lines)} packages → summary", "")
                    return {"decision": "block", "reason": summary}

    if cmd_lower.startswith("apt list") and "|" not in command:
        raw, rc = _run_cmd(command, timeout=10)
        if rc == 0 and raw:
            lines = raw.strip().split("\n")
            if len(lines) > 40:
                summary = f"{len(lines)} packages listed. Use 'apt list --installed | grep <name>' to search."
                _record_savings("bash_predict", "Bash", len(raw), len(raw) - len(summary),
                                f"apt list: {len(lines)} → count only", "")
                return {"decision": "block", "reason": summary}

    return None


# ---------------------------------------------------------------------------
# Engine: Grep Interception
# ---------------------------------------------------------------------------

def _find_rg() -> str:
    """Find ripgrep binary — could be system rg or Claude Code's bundled one."""
    # Claude Code bundles rg here
    bundled = "/usr/lib/node_modules/@anthropic-ai/claude-code/vendor/ripgrep/x64-linux/rg"
    if os.path.isfile(bundled):
        return bundled
    # System rg
    for p in ("/usr/bin/rg", "/usr/local/bin/rg"):
        if os.path.isfile(p):
            return p
    return "rg"  # Hope it's on PATH


_RG_BIN = _find_rg()


def handle_grep(tool_input: dict) -> dict | None:
    """Intercept Grep calls — run rg ourselves and compress if output is large."""
    pattern = tool_input.get("pattern", "")
    if not pattern:
        return None

    # Build rg command from tool_input
    rg_args = [_RG_BIN, "--no-heading"]
    output_mode = tool_input.get("output_mode", "files_with_matches")

    if output_mode == "files_with_matches":
        rg_args.append("-l")
    elif output_mode == "count":
        rg_args.append("-c")
    # "content" mode: default rg behavior

    if tool_input.get("-i"):
        rg_args.append("-i")
    if tool_input.get("-n", True) and output_mode == "content":
        rg_args.append("-n")
    if tool_input.get("multiline"):
        rg_args.extend(["-U", "--multiline-dotall"])

    # Context flags
    for flag in ("-A", "-B", "-C", "context"):
        val = tool_input.get(flag)
        if val and output_mode == "content":
            rg_flag = "-C" if flag == "context" else flag
            rg_args.extend([rg_flag, str(val)])

    if tool_input.get("glob"):
        rg_args.extend(["--glob", tool_input["glob"]])
    if tool_input.get("type"):
        rg_args.extend(["--type", tool_input["type"]])

    head_limit = tool_input.get("head_limit", 0)

    rg_args.append("--")
    rg_args.append(pattern)

    search_path = tool_input.get("path", ".")
    rg_args.append(search_path)

    # Run rg
    try:
        result = subprocess.run(
            rg_args, capture_output=True, text=True, timeout=10,
        )
        output = result.stdout.strip()
        if result.returncode not in (0, 1):  # 1 = no match (normal)
            return None  # Let Claude Code handle errors
    except (subprocess.TimeoutExpired, Exception):
        return None

    if not output:
        return None  # Empty results, let Claude Code handle

    # Apply head_limit if specified
    if head_limit > 0:
        lines = output.split("\n")
        if len(lines) > head_limit:
            output = "\n".join(lines[:head_limit])

    # Only compress if large
    raw_len = len(output)
    if raw_len < _MIN_COMPRESS:
        return None  # Small enough, let Claude Code run it normally

    # Cross-tool awareness: collapse results from already-read files to line numbers only
    _grep_state = _load_session_state()
    _grep_read_files = set(_grep_state.get("read_files", []))
    if _grep_read_files and output_mode == "content":
        collapsed_lines = []
        read_file_matches = {}
        other_lines = []

        for line in output.split("\n"):
            # rg format: "filepath:linenum:content" or "filepath-linenum-content"
            matched_file = None
            for sep in (":", "-"):
                if sep in line:
                    parts = line.split(sep, 2)
                    if len(parts) >= 2 and os.path.isabs(parts[0]):
                        candidate = parts[0]
                        if candidate in _grep_read_files:
                            matched_file = candidate
                            line_num = parts[1] if parts[1].isdigit() else ""
                            read_file_matches.setdefault(candidate, []).append(line_num)
                            break
            if not matched_file:
                other_lines.append(line)

        if read_file_matches:
            # Build compact version for already-read files
            for fpath, line_nums in read_file_matches.items():
                nums = [n for n in line_nums if n]
                rel = os.path.relpath(fpath, os.getcwd()) if fpath.startswith("/") else fpath
                collapsed_lines.append(
                    f"{rel}: lines {', '.join(nums[:30])}"
                    f"{f' (+{len(nums)-30} more)' if len(nums) > 30 else ''}"
                    f" [already read]"
                )

            compressed = (
                "\n".join(collapsed_lines) + "\n\n" + "\n".join(other_lines)
            ).strip()

            saved = max(0, raw_len - len(compressed))
            if saved > 500:
                _record_savings("grep_cross_tool", "Grep", raw_len, saved,
                                f"grep '{pattern[:40]}': {len(read_file_matches)} files collapsed (already read)")
                return {"decision": "block", "reason": compressed}

    # Compress using search_filter engine
    try:
        from openkeel.token_saver.engines.search_filter import filter_grep_results
        filtered, meta = filter_grep_results(output, pattern=pattern, project_root=os.getcwd())
        saved = meta.get("saved_chars", 0)
        if saved > 500:
            _record_savings("grep_compress", "Grep", raw_len, saved,
                            f"grep '{pattern[:60]}': {meta.get('original_count', '?')} → {meta.get('filtered_count', '?')} results")
            return {"decision": "block", "reason": filtered}
    except Exception:
        pass

    # v4: semantic clustering for many-match greps (runs before rule summarizer)
    if os.environ.get("TOKEN_SAVER_V4") == "1" and raw_len > _MIN_LLM_SUMMARIZE:
        try:
            from openkeel.token_saver_v4.engines import llm_engines as _v4e
            clustered = _v4e.grep_cluster(pattern, output, min_matches=30)
            if clustered and len(clustered) < raw_len * 0.5:
                saved = raw_len - len(clustered)
                _record_savings("v4_grep_cluster", "Grep", raw_len, saved,
                                f"v4 grep cluster '{pattern[:40]}': {raw_len}→{len(clustered)}")
                return {"decision": "block", "reason": clustered}
        except Exception:
            pass

    # Try LLM summarization for large grep results — aggressive threshold
    if raw_len > _MIN_LLM_SUMMARIZE:
        try:
            from openkeel.token_saver.engines.llm_calibrator import should_use_llm
            if not should_use_llm("summarization"):
                raise RuntimeError("LLM not trusted")
            from openkeel.token_saver.summarizer import summarize_grep_results
            read_ctx = ", ".join(os.path.basename(f) for f in list(_grep_read_files)[:10])
            llm_result = summarize_grep_results(pattern, output, file_context=read_ctx)
            if llm_result and len(llm_result) < raw_len * 0.6:
                saved = raw_len - len(llm_result)
                _record_savings("grep_llm_summarize", "Grep", raw_len, saved,
                                f"grep '{pattern[:40]}': LLM summarized {raw_len}→{len(llm_result)} chars")
                return {"decision": "block", "reason": (
                    f"[TOKEN SAVER] Grep results summarized by LLM ({raw_len} → {len(llm_result)} chars).\n"
                    f"Use Grep with head_limit for raw results.\n\n{llm_result}"
                )}
        except Exception:
            pass

    # Fallback: smart truncate
    compressed = _smart_truncate(output, _MAX_OUTPUT)
    saved = max(0, raw_len - len(compressed))
    if saved > 500:
        _record_savings("grep_compress", "Grep", raw_len, saved,
                        f"grep '{pattern[:60]}': truncated")
        return {"decision": "block", "reason": compressed}

    return None


# ---------------------------------------------------------------------------
# Engine: Glob Interception
# ---------------------------------------------------------------------------

def handle_glob(tool_input: dict) -> dict | None:
    """Intercept Glob calls — run glob ourselves and compress if output is large."""
    import glob as globmod

    pattern = tool_input.get("pattern", "")
    if not pattern:
        return None

    search_path = tool_input.get("path", ".")

    # Run glob
    try:
        full_pattern = os.path.join(search_path, pattern) if search_path != "." else pattern
        matches = sorted(globmod.glob(full_pattern, recursive=True))
    except Exception:
        return None

    if len(matches) < 40:
        return None  # Small enough, let Claude Code handle

    output = "\n".join(matches)
    raw_len = len(output)

    # Compress using search_filter engine
    try:
        from openkeel.token_saver.engines.search_filter import filter_glob_results
        filtered, meta = filter_glob_results(output, pattern=pattern)
        saved = meta.get("saved_chars", 0)
        if saved > 500:
            _record_savings("glob_compress", "Glob", raw_len, saved,
                            f"glob '{pattern[:60]}': {meta.get('original_count', '?')} → {meta.get('filtered_count', '?')} files")
            return {"decision": "block", "reason": filtered}
    except Exception:
        pass

    # Fallback: truncate to first 40 + count
    kept = matches[:40]
    omitted = len(matches) - 40
    compressed = "\n".join(kept) + f"\n\n... ({omitted} more files omitted)"
    saved = max(0, raw_len - len(compressed))
    if saved > 500:
        _record_savings("glob_compress", "Glob", raw_len, saved,
                        f"glob '{pattern[:60]}': truncated {len(matches)} → 40")
        return {"decision": "block", "reason": compressed}

    return None


# ---------------------------------------------------------------------------
# Engine: Agent Prompt Trimming
# ---------------------------------------------------------------------------

def handle_agent(tool_input: dict) -> dict | None:
    """Track agent spawns and trim redundant context from prompts.

    Agent prompts often repeat information already in the conversation.
    We can't modify the prompt, but we can detect and log waste, and
    for very large prompts, we block with a note to use a shorter prompt.
    """
    prompt = tool_input.get("prompt", "")
    prompt_len = len(prompt)

    # Log agent spawn for analysis
    _record_savings("agent_spawn", "Agent", prompt_len, 0,
                    f"agent: {tool_input.get('description', '')[:80]}, prompt: {prompt_len} chars")

    # For extremely long agent prompts (>8K chars), suggest trimming
    # but don't block — agents are expensive to re-prompt
    return None


# ---------------------------------------------------------------------------
# Engine: Edit Trimming — shrink oversized old_string to minimal unique match
# ---------------------------------------------------------------------------

def handle_edit(tool_input: dict) -> dict | None:
    """Trim oversized Edit old_string to the minimal unique substring.

    When Claude sends a 200-line old_string to change 1 line, we find the
    smallest unique substring that still matches exactly once, and rewrite
    the Edit call. This doesn't block — it rewrites tool_input in place
    so Claude Code executes a smaller edit.

    Since pre_tool can only block (not rewrite), we do the edit ourselves
    and return the confirmation, saving the full file echo.
    """
    file_path = tool_input.get("file_path", "")
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")

    if not file_path or not old_string:
        return None

    # Read the file to check size
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None

    # Intercept if EITHER condition is met:
    #   1. old_string is large (>150 chars) — Claude sent too much context
    #   2. File is large (>6KB) — the Edit confirmation echo will be huge
    file_is_large = len(content) > 6000
    old_is_large = len(old_string) > 150

    if not file_is_large and not old_is_large:
        return None

    # Verify old_string exists exactly once
    count = content.count(old_string)
    if count != 1:
        return None  # Let Claude Code handle the error

    # Do the replacement ourselves
    new_content = content.replace(old_string, new_string, 1)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError:
        return None  # Let Claude Code try

    # Build a compact confirmation (like Edit tool but without echoing the file)
    old_lines = old_string.count("\n") + 1
    new_lines = new_string.count("\n") + 1
    saved = len(old_string) + len(new_string) + len(content)  # What Edit would have cost
    actual = 200  # Our compact response

    _record_savings(
        "edit_trim", "Edit", saved, max(0, saved - actual),
        f"edit_trim: {os.path.basename(file_path)} ({old_lines}L→{new_lines}L, "
        f"trimmed {len(old_string)} chars old_string)",
        file_path,
    )

    # Show a compact diff instead of the full file
    # Find where the change happened
    pos = content.find(old_string)
    line_num = content[:pos].count("\n") + 1

    # Show 3 lines of context around the change
    old_preview = old_string[:150].replace("\n", "\\n")
    new_preview = new_string[:150].replace("\n", "\\n")

    return {
        "decision": "block",
        "reason": (
            f"[TOKEN SAVER \u2713 EDIT APPLIED] The file has already been written. "
            f"Do NOT retry this Edit call.\n"
            f"File: {os.path.relpath(file_path, '/home/om/openkeel')}\n"
            f"Summary: {old_lines} lines changed at line {line_num} "
            f"(old_string was {len(old_string)} chars; token saver performed the write "
            f"directly to save tokens). Treat this as SUCCESS, not an error."
        ),
    }


# ---------------------------------------------------------------------------
# Engine: Write Trim
# ---------------------------------------------------------------------------

def handle_write(tool_input: dict) -> dict | None:
    """Intercept Write calls on large files — perform the write ourselves and return compact confirmation."""
    file_path = tool_input.get("file_path", "")
    content = tool_input.get("content", "")

    if not file_path or not content:
        return None

    # Only intercept if content is large enough to matter
    if len(content) < 3000:
        return None

    try:
        line_count = len(content.split("\n"))
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        rel = os.path.relpath(file_path, os.getcwd()) if file_path.startswith("/") else file_path
        msg = (
            f"File created successfully at: {file_path}\n"
            f"[TOKEN SAVER] Write trimmed: {line_count} lines, {len(content)} chars written to {rel}."
        )

        _record_savings(
            "write_trim", "Write", len(content), max(0, len(content) - len(msg)),
            f"write_trim: {rel} ({line_count}L)", file_path,
        )
        return {"decision": "block", "reason": msg}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    result = None

    try:
        if tool_name == "Read":
            result = handle_read(tool_input)
        elif tool_name == "Edit":
            result = handle_edit(tool_input)
        elif tool_name == "Write":
            result = handle_write(tool_input)
        elif tool_name == "Bash":
            result = handle_bash(tool_input)
        elif tool_name == "Grep":
            result = handle_grep(tool_input)
        elif tool_name == "Glob":
            result = handle_glob(tool_input)
        elif tool_name == "Agent":
            result = handle_agent(tool_input)
    except Exception:
        pass  # Fail-open

    # --- v4 shim: post-process the 'reason' field through lingua_compressor ---
    # Only fires when TOKEN_SAVER_V4=1. Fails open — any exception falls through
    # to the unmodified v3 result so v4 can never break the live hook path.
    if result and os.environ.get("TOKEN_SAVER_V4") == "1":
        try:
            reason = result.get("reason") if isinstance(result, dict) else None
            if isinstance(reason, str) and len(reason) >= 400:
                from openkeel.token_saver_v4.engines import lingua_compressor as _v4lc
                _cr = _v4lc.compress(reason)
                if _cr.saved_chars > 0 and _cr.compressed:
                    result["reason"] = _cr.compressed
                    try:
                        _record_savings(
                            "v4_lingua_prehook",
                            tool_name,
                            _cr.original_chars,
                            _cr.saved_chars,
                            notes=f"mode={_cr.mode}",
                        )
                    except Exception:
                        pass
        except Exception:
            pass  # v4 never blocks v3

    if result:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
