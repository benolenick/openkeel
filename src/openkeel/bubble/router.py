"""Task routing — decide whether to use bubble or go vanilla."""

import re


def should_use_bubble(task):
    """Heuristic: skip bubble for tasks vanilla handles in <4 turns.

    Returns True if bubble is likely to save tokens, False if vanilla is better.
    """
    task_lower = task.lower()

    # Broad mapping tasks — vanilla is often more efficient
    broad_markers = [
        "complete data model",
        "map the complete",
        "every in-memory",
        "every global variable",
        "all tables and all",
        "entire codebase",
        "full architecture",
    ]
    if any(m in task_lower for m in broad_markers):
        return False

    # Very simple lookups (no specific file mentioned)
    simple_markers = ["list all", "count how many", "show me the"]
    specific_file = bool(re.search(r"\b\w+\.\w{2,4}\b", task))
    if any(m in task_lower for m in simple_markers) and not specific_file:
        return False

    return True
