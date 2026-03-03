"""Rule loading and dataclasses for constitution enforcement."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class RuleMatch:
    """Defines what a rule matches against."""
    field: str  # e.g. "command" for Bash tool, "file_path" for Read/Write/Edit
    pattern: str  # regex pattern
    _compiled: re.Pattern | None = field(default=None, repr=False, compare=False)

    @property
    def compiled(self) -> re.Pattern:
        if self._compiled is None:
            self._compiled = re.compile(self.pattern)
        return self._compiled


@dataclass
class Rule:
    """A single constitution rule."""
    id: str
    tool: str  # Tool name to match: "Bash", "Write", "Edit", "*" for all
    match: RuleMatch
    action: str = "deny"  # "deny", "alert", "allow"
    message: str = ""
    when_tags: list[str] = field(default_factory=list)  # Only active when mission has these tags


def load_rules(path: str | Path) -> list[Rule]:
    """Load rules from a YAML file. Returns empty list if file doesn't exist."""
    path = Path(path).expanduser()
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict) or "rules" not in data:
        return []

    rules = []
    for raw in data["rules"]:
        match_data = raw.get("match", {})
        match = RuleMatch(
            field=match_data.get("field", "command"),
            pattern=match_data.get("pattern", ""),
        )
        rule = Rule(
            id=raw.get("id", "unnamed"),
            tool=raw.get("tool", "*"),
            match=match,
            action=raw.get("action", "deny"),
            message=raw.get("message", ""),
            when_tags=raw.get("when_tags", []),
        )
        rules.append(rule)

    return rules
