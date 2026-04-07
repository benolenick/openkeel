"""Subagent Offload — nudge (not force) the main agent to delegate exploration.

Watches the recent tool-call history. If the session is in an "exploration
chain" (many Grep/Glob/Read calls in a row without an Edit/Write), it emits
a nudge string that the pre_tool hook can inject as an assistant-facing hint.

v4.0 policy: NUDGE ONLY. Never auto-spawns. Ben pushed back on auto-spawn
(rightly — most bloat is already caught by tool-result summarization), so
this layer just surfaces the option when the pattern is obvious.

The caller feeds recent tool events and gets back either None (no nudge) or
a short string to inject.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

EXPLORATION_TOOLS = {"Grep", "Glob", "Read"}
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
MIN_CHAIN = 5           # need at least this many exploration calls
MAX_LOOKBACK = 15       # only look at the most recent N events
COOLDOWN_EVENTS = 20    # don't nudge again for this many events after one fires


@dataclass
class NudgeDecision:
    should_nudge: bool
    reason: str
    chain_len: int
    message: str = ""


def _format_nudge(chain_len: int) -> str:
    return (
        f"[token-saver v4] You've run {chain_len} exploration calls "
        f"(Grep/Glob/Read) in a row without editing anything. If you're "
        f"still searching for where something lives, consider delegating "
        f"the rest of the search to `Agent(subagent_type=\"Explore\")` "
        f"so the results don't pollute your main context. This is a "
        f"suggestion, not an order."
    )


def evaluate(
    recent_tools: Iterable[str],
    events_since_last_nudge: int = 999,
) -> NudgeDecision:
    """Decide whether to nudge, given a sequence of recent tool names (newest last).

    Pure function — no state, no I/O. The caller tracks cooldown.
    """
    tools = list(recent_tools)[-MAX_LOOKBACK:]

    if events_since_last_nudge < COOLDOWN_EVENTS:
        return NudgeDecision(False, "cooldown", 0)

    # Walk backwards; count trailing exploration calls until we hit a write
    chain = 0
    for t in reversed(tools):
        if t in WRITE_TOOLS:
            break
        if t in EXPLORATION_TOOLS:
            chain += 1
        else:
            # Some other tool (Bash, Task, etc.) — neutral, keep counting
            continue

    if chain >= MIN_CHAIN:
        return NudgeDecision(
            should_nudge=True,
            reason=f"exploration_chain_{chain}",
            chain_len=chain,
            message=_format_nudge(chain),
        )
    return NudgeDecision(False, "no_chain", chain)
