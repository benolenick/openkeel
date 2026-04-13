"""Haiku-powered data gathering for the bubble pattern (v4 — local LLM + Hyphae)."""

import json
import os
import re
import subprocess
import time

import httpx

from .config import get_api_key, get_config
from openkeel.hyphae import client as hyphae
from . import ollama as local_llm

# Budget caps
MAX_GATHER_CHARS = 25_000
MAX_EXTRA_ROUNDS = 2
THIN_THRESHOLD = 200

TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file. Returns contents with line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 2000},
            },
            "required": ["path"],
        },
    },
    {
        "name": "bash",
        "description": "Run a bash command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]


def execute_tool(name, input_data):
    """Execute a tool call locally."""
    if name == "read_file":
        path = input_data.get("path", "")
        offset = input_data.get("offset", 0)
        limit = input_data.get("limit", 2000)
        try:
            with open(path) as f:
                lines = f.readlines()
            selected = lines[offset : offset + limit]
            return "".join(
                f"{i + offset + 1}\t{line}" for i, line in enumerate(selected)
            )
        except Exception as e:
            return f"Error: {e}"
    elif name == "bash":
        cmd = input_data.get("command") or str(input_data)
        try:
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=30
            )
            out = r.stdout + r.stderr
            return out[:8000] if out else "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: command timed out (30s)"
        except Exception as e:
            return f"Error: {e}"
    return f"Unknown tool: {name}"


def haiku_api(prompt, system, max_tokens=1024, max_rounds=5, tools=None):
    """Call Haiku via direct API. Returns (text, input_tok, output_tok, cost)."""
    cfg = get_config()
    api_key = get_api_key()
    messages = [{"role": "user", "content": prompt}]
    all_text = []
    total_in = 0
    total_out = 0

    for _ in range(max_rounds):
        payload = {
            "model": cfg["gather_model"],
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools

        try:
            resp = httpx.post(
                cfg["api_url"],
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": cfg["api_version"],
                    "content-type": "application/json",
                },
                json=payload,
                timeout=60.0,
            )
        except Exception as e:
            all_text.append(f"[API error: {e}]")
            break

        if resp.status_code != 200:
            all_text.append(f"[API {resp.status_code}: {resp.text[:200]}]")
            break

        data = resp.json()
        usage = data.get("usage", {})
        total_in += usage.get("input_tokens", 0)
        total_out += usage.get("output_tokens", 0)

        tool_uses = []
        for block in data.get("content", []):
            if block["type"] == "text" and block.get("text", "").strip():
                all_text.append(block["text"])
            elif block["type"] == "tool_use":
                tool_uses.append(block)

        if data.get("stop_reason") == "end_turn" or not tool_uses:
            break

        messages.append({"role": "assistant", "content": data["content"]})
        tool_results = []
        for tu in tool_uses:
            result = execute_tool(tu["name"], tu["input"])
            all_text.append(result[:4000])
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": result[:8000],
                }
            )
        messages.append({"role": "user", "content": tool_results})

    cost = (total_in * 0.80 + total_out * 4.00) / 1_000_000

    # Emit token event for GUI dials
    try:
        from openkeel.token_events import emit as _emit_tok
        _emit_tok("haiku", total_in, total_out)
    except Exception:
        pass

    return "\n".join(all_text), total_in, total_out, cost


def _plan_local(task, repo_tree, hyphae_context, model):
    """Plan gather commands using a local LLM via Ollama.

    Returns (commands_list, elapsed_ms). Cost is always 0.
    """
    hyphae_section = ""
    if hyphae_context:
        hyphae_section = (
            f"\n## Prior Knowledge (from project memory)\n"
            f"{hyphae_context[:3000]}\n"
        )

    prompt = (
        f"You are planning data gathering for a code analysis task.\n\n"
        f"## Repository file listing (use EXACT paths from this list):\n```\n{repo_tree[:6000]}\n```\n"
        f"{hyphae_section}\n"
        f"Task: {task}\n\n"
        f"CRITICAL RULES:\n"
        f"1. You MUST include cat or grep commands that READ FILE CONTENTS. Listing files alone is NEVER enough.\n"
        f"2. At least 2 commands must be 'cat <filepath>' or 'grep -n <pattern> <filepath>' on files from the listing.\n"
        f"3. Pick the most relevant files for the task and cat them.\n\n"
        f"EXAMPLE — if the task asks about a function in config.py:\n"
        f'["cat /repo/src/config.py", "grep -n \\"function_name\\" /repo/src/config.py", "grep -rn \\"function_name\\" /repo/src/"]\n\n'
        f"List 3-6 bash commands using EXACT paths from the listing above.\n"
        f"Reply with ONLY a JSON array of command strings."
    )

    text, elapsed = local_llm.generate(
        prompt, model,
        system="You plan bash commands for code analysis. You MUST include cat or grep commands that read file contents — never just find or ls. Reply with only a JSON array.",
        max_tokens=512,
    )

    # Parse commands from local model output
    try:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
        commands = json.loads(cleaned)
    except (json.JSONDecodeError, IndexError):
        match = re.search(r"\[.*\]", text or "", re.DOTALL)
        if match:
            try:
                commands = json.loads(match.group())
            except json.JSONDecodeError:
                commands = []
        else:
            commands = []

    return commands, elapsed


def get_repo_tree(repo_path):
    """Get compact file listing for spatial awareness."""
    try:
        # -maxdepth 6 prevents runaway nested dirs; awk deduplicates by basename
        r = subprocess.run(
            f"find {repo_path} -maxdepth 6 -type f \\( -name '*.py' -o -name '*.sql' -o -name '*.js' "
            f"-o -name '*.ts' -o -name '*.yaml' -o -name '*.yml' -o -name '*.toml' "
            f"-o -name '*.go' -o -name '*.rs' -o -name '*.java' -o -name '*.rb' "
            f"-o -name '*.php' -o -name '*.c' -o -name '*.h' -o -name '*.cpp' \\) "
            f"| grep -v __pycache__ | grep -v node_modules | grep -v .git "
            f"| grep -v vendor | grep -v venv | grep -v .env "
            f"| awk -F/ '!seen[$NF]++' "
            f"| sort | head -150",
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def validate_commands(commands, repo_path):
    """Check that referenced paths actually exist. Remove bad commands."""
    valid = []
    for cmd in commands:
        paths_in_cmd = re.findall(
            r"(/\S+\.(?:py|sql|js|ts|yaml|yml|toml|json|md|go|rs|java|rb|php|c|h|cpp))",
            cmd,
        )
        if not paths_in_cmd:
            valid.append(cmd)
            continue
        any_exists = any(os.path.exists(p) for p in paths_in_cmd)
        if any_exists:
            valid.append(cmd)
    return valid if valid else commands[:2]


def gather(task, repo_path, hyphae_context="", local_model=None):
    """Plan + execute data gathering. Uses local LLM or Haiku API.

    Returns (gathered_text, cost, details_dict).
    """
    t0 = time.time()

    repo_tree = get_repo_tree(repo_path)

    if local_model:
        # v4: Local LLM planning — zero API cost
        commands, plan_ms = _plan_local(task, repo_tree, hyphae_context, local_model)
        commands = validate_commands(commands, repo_path)
        p_cost = 0
        plan_tokens = {"in": 0, "out": 0, "local_model": local_model, "plan_ms": plan_ms}
    else:
        # v3: Haiku API planning
        hyphae_section = ""
        if hyphae_context:
            hyphae_section = (
                f"\n## Prior Knowledge (from project memory)\n"
                f"{hyphae_context[:3000]}\n"
            )

        plan_prompt = (
            f"You are planning data gathering for a code analysis task.\n\n"
            f"## Repository: {repo_path}\n\n"
            f"## File listing (use EXACT paths from this list):\n```\n{repo_tree[:6000]}\n```\n"
            f"{hyphae_section}\n"
            f"Task: {task}\n\n"
            f"List 3-6 specific bash commands (grep, find, cat with line ranges, head) that would gather the data needed.\n"
            f"IMPORTANT: Use exact file paths from the listing above. Do NOT guess paths.\n"
            f"If the prior knowledge mentions relevant files or patterns, prioritize those.\n"
            f"Reply with ONLY a JSON array of command strings."
        )

        plan_result, p_in, p_out, p_cost = haiku_api(
            plan_prompt,
            system="Reply with only a JSON array of bash command strings. No explanation. Use exact paths from the file listing provided.",
            max_tokens=512,
            max_rounds=1,
        )

        # Parse commands
        try:
            cleaned = plan_result.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
            commands = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError):
            match = re.search(r"\[.*\]", plan_result or "", re.DOTALL)
            if match:
                try:
                    commands = json.loads(match.group())
                except json.JSONDecodeError:
                    commands = [f"find {repo_path} -name '*.py' | head -20"]
            else:
                commands = [f"find {repo_path} -name '*.py' | head -20"]

        commands = validate_commands(commands, repo_path)
        plan_tokens = {"in": p_in, "out": p_out}

    # Execute with budget cap
    results = []
    cmd_details = []
    total_gathered = 0
    for i, cmd in enumerate(commands[:6]):
        if total_gathered > MAX_GATHER_CHARS:
            cmd_details.append({"cmd": cmd[:200], "skipped": "budget_exceeded"})
            continue
        ct0 = time.time()
        try:
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=30
            )
            out = r.stdout + r.stderr
            capped = out[:5000]
            results.append(f"### Command {i + 1}: {cmd}\n{capped}")
            total_gathered += len(capped)
            cmd_details.append(
                {
                    "cmd": cmd[:200],
                    "len": len(out),
                    "ms": round((time.time() - ct0) * 1000),
                }
            )
        except Exception as e:
            results.append(f"### Command {i + 1}: {cmd}\nError: {e}")
            cmd_details.append({"cmd": cmd[:200], "error": str(e)})

    gathered = "\n\n".join(results)

    # Extra pass if truly empty
    extra_cost = 0
    if len(gathered) < THIN_THRESHOLD:
        if local_model:
            # Local extra pass — ask for more commands
            extra_text, _ = local_llm.generate(
                f"The previous commands returned almost no data. Suggest 2-3 more bash commands to gather data for:\n"
                f"Task: {task}\n"
                f"Available files:\n```\n{repo_tree[:4000]}\n```\n"
                f"Reply with ONLY a JSON array of command strings.",
                local_model,
                system="Reply with only a JSON array of bash command strings.",
                max_tokens=256,
            )
            try:
                extra_cmds = json.loads(re.search(r"\[.*\]", extra_text or "", re.DOTALL).group())
            except Exception:
                extra_cmds = [f"find {repo_path} -name '*.py' | head -20"]
            for cmd in extra_cmds[:3]:
                try:
                    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                    out = r.stdout + r.stderr
                    if out.strip():
                        gathered += f"\n\n### Extra: {cmd}\n{out[:5000]}"
                except Exception:
                    pass
        else:
            extra_result, e_in, e_out, e_cost = haiku_api(
                f"Gather data for this task. Work in {repo_path}.\n\n"
                f"Available files:\n```\n{repo_tree[:4000]}\n```\n\n"
                f"Task: {task}",
                system=(
                    f"You are a silent data-gathering tool. Execute commands and read files. "
                    f"Return ONLY raw output. Use exact paths from the file listing. "
                    f"Work inside: {repo_path}"
                ),
                max_tokens=1024,
                max_rounds=MAX_EXTRA_ROUNDS,
                tools=TOOLS,
            )
            if extra_result:
                gathered += f"\n\n### Extra gather:\n{extra_result}"
                extra_cost = e_cost

    # Quality check
    error_lines = sum(
        1
        for line in gathered.split("\n")
        if line.strip().startswith("Error:") or "No such file" in line
    )
    total_lines = max(len(gathered.split("\n")), 1)
    gather_quality = "good" if error_lines / total_lines < 0.5 else "poor"

    return (
        gathered,
        p_cost + extra_cost,
        {
            "commands": cmd_details,
            "plan_tokens": plan_tokens,
            "gathered_len": len(gathered),
            "elapsed_ms": round((time.time() - t0) * 1000),
            "gather_quality": gather_quality,
            "had_repo_tree": bool(repo_tree),
            "had_hyphae": bool(hyphae_context),
            "local_gather": local_model or False,
        },
    )
