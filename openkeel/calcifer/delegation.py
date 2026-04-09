#!/usr/bin/env python3
"""Delegation: Opus tells Calcifer to spawn a sub-agent.

When Opus emits a delegate() tool call, Calcifer:
1. Spawns a sub-agent loop with the specified runner
2. Lets it work until done
3. Summarizes the output
4. Returns summary to Opus
"""

from __future__ import annotations

from dataclasses import dataclass
from openkeel.calcifer.agent_loop import run_agent_loop, AgentLoopConfig, LoopResult
from openkeel.calcifer.intention_broker import get_broker


@dataclass
class DelegationRequest:
    """What Opus asks Calcifer to do."""

    runner: str                             # "sonnet", "haiku", "gemma4_large", etc.
    task_spec: str                          # what to do
    budget_turns: int = 10
    summary_style: str = "technical"        # "technical", "brief", "detailed"
    context_lines: list[str] = None         # extra context for the sub-agent


def _model_for_runner(runner: str) -> str:
    """Map runner name to Anthropic model ID."""
    mapping = {
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5-20251001",
        "opus": "claude-opus-4-6",
        "gemma4_small": "gemma4:e2b",
        "gemma4_large": "gemma4:26b",
        "qwen25": "qwen2.5:3b",
    }
    return mapping.get(runner, runner)


def _sub_agent_system_prompt(intention_brief: str, style: str = "technical") -> str:
    """Build system prompt for a sub-agent."""
    instruction = {
        "technical": "Provide technical details, code snippets, and specific findings.",
        "brief": "Be concise. Focus on the outcome, not the process.",
        "detailed": "Explain your reasoning at each step. Show your work.",
    }.get(style, "Provide clear, actionable results.")

    return f"""You are a specialized agent working on a delegated task.

{intention_brief}

Your task: Complete the work below using available tools.
{instruction}

Rules:
- Use tools to investigate and make changes
- Report what you found and what you changed
- If you hit a blocker, explain it clearly
- Do NOT ask for clarification; make reasonable assumptions

When done, summarize your work in a final text message."""


def delegate(
    request: DelegationRequest,
    session_id: str,
    intention_brief: str = "",
) -> str:
    """Execute a delegation request.

    Returns: summary of the sub-agent's work (for Opus to see)
    """
    model = _model_for_runner(request.runner)
    system_prompt = _sub_agent_system_prompt(intention_brief, request.summary_style)

    context_str = ""
    if request.context_lines:
        context_str = "\n".join(request.context_lines) + "\n\n"

    user_message = f"{context_str}{request.task_spec}"

    config = AgentLoopConfig(
        model=model,
        system_prompt=system_prompt,
        user_message=user_message,
        max_turns=request.budget_turns,
    )

    result = run_agent_loop(config)

    # Record discovery in intention packet
    broker = get_broker()
    if result.success:
        broker.record_discovery(
            session_id,
            f"[{request.runner}] {result.final_response[:200]}"
        )

    # Return the final response as the "tool result" for Opus
    return result.final_response


def parse_delegate_tool_call(tool_use: dict) -> DelegationRequest | None:
    """Parse a tool_use block that might be a delegation request.

    Opus emits something like:
    {
        "type": "tool_use",
        "name": "delegate",
        "id": "...",
        "input": {
            "runner": "sonnet",
            "task_spec": "...",
            "budget_turns": 10,
            ...
        }
    }

    Returns DelegationRequest if it's a delegation, None otherwise.
    """
    if tool_use.get("name") != "delegate":
        return None

    inp = tool_use.get("input", {})
    return DelegationRequest(
        runner=inp.get("runner", "sonnet"),
        task_spec=inp.get("task_spec", ""),
        budget_turns=inp.get("budget_turns", 10),
        summary_style=inp.get("summary_style", "technical"),
    )
