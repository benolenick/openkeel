"""Rule evaluation engine for constitution enforcement."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .rules import Rule

logger = logging.getLogger(__name__)


@dataclass
class RuleResult:
    """Result of evaluating rules against a tool call."""
    action: str  # "allow", "deny", "alert"
    rule_id: str  # ID of the matched rule, or "" if no match
    message: str  # Human-readable message


def evaluate(
    rules: list[Rule],
    tool_name: str,
    tool_input: dict[str, Any],
    active_tags: list[str] | None = None,
) -> RuleResult:
    """Evaluate rules against a tool call. First match wins.

    Args:
        rules: List of rules to evaluate (order matters - first match wins)
        tool_name: The tool being called (e.g. "Bash", "Write", "Edit")
        tool_input: The tool's input parameters as a dict
        active_tags: Tags from the active mission (for when_tags filtering)

    Returns:
        RuleResult with action="allow" if no rule matched (default-allow)
    """
    if active_tags is None:
        active_tags = []

    for rule in rules:
        # Check tool name match
        if rule.tool != "*" and rule.tool != tool_name:
            continue

        # Check when_tags - rule only applies if ALL its tags are in active_tags
        if rule.when_tags and not all(tag in active_tags for tag in rule.when_tags):
            continue

        # Get the field value to match against
        field_value = tool_input.get(rule.match.field, "")
        if not isinstance(field_value, str):
            field_value = json.dumps(field_value)

        # Try regex match
        if rule.match.compiled.search(field_value):
            logger.debug("Rule %s matched on %s.%s", rule.id, tool_name, rule.match.field)
            return RuleResult(
                action=rule.action,
                rule_id=rule.id,
                message=rule.message or f"Rule {rule.id}: {rule.action}",
            )

    # Default: allow if no rule matched
    return RuleResult(action="allow", rule_id="", message="")
