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
import urllib.error
import urllib.request

DAEMON_URL = os.environ.get("TOKEN_SAVER_DAEMON", "http://127.0.0.1:11450")
EDITED_FILES_PATH = os.path.expanduser("~/.openkeel/scribe_state.json")

# Minimum output size (chars) before we bother compressing
_MIN_COMPRESS = 1500
# Max chars to return after compression
_MAX_OUTPUT = 3000


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
    These are often 10-30k chars. We keep top results by score, truncate text.
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
        compressed_results = []
        for r in results[:10]:  # Keep top 10
            if isinstance(r, dict):
                text = r.get("text", "")
                # Truncate long texts to 500 chars
                if len(text) > 500:
                    text = text[:500] + "..."
                compressed_results.append({
                    "text": text,
                    "score": round(r.get("score", 0), 3),
                    "source": r.get("source", ""),
                })
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
        return None

    # If this is a re-read, try to serve a summary
    already_read = _check_session_read(file_path)
    if not already_read:
        return None

    result = _daemon_post("/summarize", {"path": file_path})
    if not result or not result.get("summary"):
        return None

    summary = result["summary"]
    orig_lines = result.get("original_lines", 0)
    orig_chars = result.get("original_chars", 0)
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


def handle_bash(tool_input: dict) -> dict | None:
    command = tool_input.get("command", "").strip()
    if not command:
        return None

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

    # --- Hyphae API: curl to port 8100 (avg 13.7k chars, #1 consumer) ---
    if "8100" in command and "curl" in cmd_lower:
        return _run_and_compress(command, timeout=15, label="hyphae", json_compress=True)

    # --- Kanban API: curl to port 8200 ---
    if "8200" in command and "curl" in cmd_lower:
        return _run_and_compress(command, timeout=10, label="kanban", json_compress=True)

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
    if any(cmd_lower.startswith(p) for p in ("ps aux", "ps -", "systemctl list", "systemctl --user list")):
        return _run_and_compress(command, timeout=5, label="process list")

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
        elif tool_name == "Bash":
            result = handle_bash(tool_input)
    except Exception:
        pass  # Fail-open

    if result:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
