#!/usr/bin/env python3
"""Reusable agent loop for any runner (Opus, Sonnet, Haiku, local).

The core loop: send context → get response → execute tools → append results → repeat.
Works the same way for all models. This is the engine that powers sub-agent delegation.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Optional, Callable, Any
import anthropic
import ollama


@dataclass
class AgentLoopConfig:
    """Configuration for an agent loop run."""

    model: str                              # "claude-opus-4-6", "sonnet-4-6", "haiku", "gemma4:e2b", etc.
    system_prompt: str                      # the governor's instructions
    user_message: str                       # the task or question
    history: list[dict] = None              # prior messages if continuing
    max_turns: int = 10                     # safety limit
    max_budget_tokens: int = 50000         # total token spend limit
    callbacks: Optional[Callable] = None    # for progress reporting


@dataclass
class LoopResult:
    """What an agent loop returns."""

    success: bool
    final_response: str                     # last text output from model
    messages: list[dict]                    # full conversation history (for resuming)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    turn_count: int = 0
    tool_calls_made: int = 0


# ── Tool implementations ───────────────────────────────────────────────────────

def _bash(cmd: str) -> str:
    """Execute a shell command. Returns stdout + stderr combined."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        return result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return "[TIMEOUT] Command took longer than 30 seconds"
    except Exception as e:
        return f"[ERROR] {str(e)}"


def _read(path: str) -> str:
    """Read a file. Returns first 5000 chars."""
    try:
        with open(path, "r") as f:
            content = f.read(5000)
        return content
    except Exception as e:
        return f"[ERROR reading {path}] {str(e)}"


def _write(path: str, content: str) -> str:
    """Write a file."""
    try:
        with open(path, "w") as f:
            f.write(content)
        return f"[OK] Wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"[ERROR writing {path}] {str(e)}"


def _glob(pattern: str) -> str:
    """Find files matching pattern."""
    try:
        import glob
        matches = glob.glob(pattern, recursive=True)
        return "\n".join(matches[:50])  # limit to 50 results
    except Exception as e:
        return f"[ERROR] {str(e)}"


def _grep(pattern: str, path: str = ".") -> str:
    """Search for pattern in files."""
    try:
        result = subprocess.run(
            f"grep -r '{pattern}' {path} --include='*.py' --include='*.md'",
            shell=True, capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.split("\n")[:50]  # limit
        return "\n".join(lines)
    except Exception as e:
        return f"[ERROR] {str(e)}"


TOOLS = {
    "bash": {
        "name": "bash",
        "description": "Execute a shell command",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"}
            },
            "required": ["command"],
        },
    },
    "read": {
        "name": "read",
        "description": "Read a file",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"}
            },
            "required": ["path"],
        },
    },
    "write": {
        "name": "write",
        "description": "Write a file",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "File content"},
            },
            "required": ["path", "content"],
        },
    },
    "glob": {
        "name": "glob",
        "description": "Find files matching a pattern",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g., '**/*.py')"}
            },
            "required": ["pattern"],
        },
    },
    "grep": {
        "name": "grep",
        "description": "Search for pattern in files",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Pattern to search for"},
                "path": {"type": "string", "description": "Path to search in (default: '.')"},
            },
            "required": ["pattern"],
        },
    },
}


def _execute_tool(name: str, **kwargs) -> str:
    """Execute a single tool call."""
    if name == "bash":
        return _bash(kwargs["command"])
    elif name == "read":
        return _read(kwargs["path"])
    elif name == "write":
        return _write(kwargs["path"], kwargs["content"])
    elif name == "glob":
        return _glob(kwargs["pattern"])
    elif name == "grep":
        return _grep(kwargs["pattern"], kwargs.get("path", "."))
    else:
        return f"[ERROR] Unknown tool: {name}"


# ── Opus/Sonnet/Haiku runner ──────────────────────────────────────────────────

def _run_anthropic_loop(config: AgentLoopConfig) -> LoopResult:
    """Run agent loop for Opus, Sonnet, Haiku."""
    client = anthropic.Anthropic()
    messages = config.history or []
    messages.append({"role": "user", "content": config.user_message})

    result = LoopResult(
        success=False,
        final_response="",
        messages=messages,
    )

    for turn in range(1, config.max_turns + 1):
        resp = client.messages.create(
            model=config.model,
            max_tokens=4096,
            system=config.system_prompt,
            tools=[TOOLS[name] for name in TOOLS.keys()],
            messages=messages,
        )

        result.total_input_tokens += resp.usage.input_tokens
        result.total_output_tokens += resp.usage.output_tokens
        result.turn_count = turn

        # Extract text and tool use from response
        text_blocks = [b.text for b in resp.content if hasattr(b, "text")]
        tool_blocks = [b for b in resp.content if b.type == "tool_use"]

        if text_blocks:
            result.final_response = text_blocks[-1]

        # Append assistant response
        messages.append({"role": "assistant", "content": resp.content})

        # If no tool calls, we're done
        if not tool_blocks:
            result.success = True
            break

        # Execute tools and collect results
        result.tool_calls_made += len(tool_blocks)
        tool_results = []
        for tool_block in tool_blocks:
            output = _execute_tool(tool_block.name, **tool_block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": output,
            })

        # Append tool results and loop
        messages.append({"role": "user", "content": tool_results})

        if config.callbacks:
            config.callbacks(f"turn {turn}: {len(tool_blocks)} tools called")

    result.messages = messages
    return result


# ── Local model runner (Ollama) ────────────────────────────────────────────────

def _run_local_loop(config: AgentLoopConfig) -> LoopResult:
    """Run agent loop for local models (Gemma, Qwen via Ollama)."""
    # Note: Local models don't have native tool_use support like Claude.
    # For now, return a minimal result. In production, we'd use function calling
    # or a prompt-based approach to make local models emit tool calls.

    client = ollama.Client(host="http://127.0.0.1:11434")
    messages = config.history or []
    messages.append({"role": "user", "content": config.user_message})

    result = LoopResult(
        success=False,
        final_response="",
        messages=messages,
    )

    # Local models run in a single turn (no tool loop) for now
    try:
        resp = client.chat(
            model=config.model,
            messages=messages,
            stream=False,
        )
        result.final_response = resp["message"]["content"]
        result.success = True
        result.turn_count = 1
        messages.append({"role": "assistant", "content": result.final_response})
    except Exception as e:
        result.final_response = f"[ERROR] Local model failed: {str(e)}"

    result.messages = messages
    return result


# ── Main entry point ──────────────────────────────────────────────────────────

def run_agent_loop(config: AgentLoopConfig) -> LoopResult:
    """Run an agent loop for any model."""
    if config.model.startswith("claude-"):
        return _run_anthropic_loop(config)
    else:
        # Local model (ollama)
        return _run_local_loop(config)
