#!/usr/bin/env python3
"""DirectRunner: ultra-cheap deterministic operations (no model needed)."""

from openkeel.calcifer.contracts import StepSpec, StatusPacket


class DirectRunner:
    """Execute deterministic steps: file checks, string manipulations, etc."""

    def execute(self, step: StepSpec) -> StatusPacket:
        """Execute a direct step (no LLM)."""
        actions = []
        artifacts = []
        checks_passed = []

        # Step kinds: "read", "grep", "list_dir", "check_syntax"
        if step.step_kind == "read":
            path = step.inputs.get("path")
            try:
                with open(path) as f:
                    content = f.read()
                actions.append(f"read {path} ({len(content)} bytes)")
                artifacts.append(path)
                summary = f"Read {path}:\n{content[:500]}{'...' if len(content) > 500 else ''}"
                checks_passed.append(("file_readable", True, path))
            except Exception as e:
                summary = f"Failed to read {path}: {e}"
                checks_passed.append(("file_readable", False, str(e)))

        elif step.step_kind == "grep":
            import subprocess
            pattern = step.inputs.get("pattern")
            path = step.inputs.get("path", ".")
            try:
                result = subprocess.run(
                    ["grep", "-r", pattern, path],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                matches = result.stdout.count("\n")
                actions.append(f"grep {pattern} in {path}: {matches} matches")
                summary = f"Found {matches} matches:\n{result.stdout[:500]}"
                checks_passed.append(("grep_found", matches > 0, f"{matches} matches"))
            except Exception as e:
                summary = f"Grep failed: {e}"
                checks_passed.append(("grep_found", False, str(e)))

        elif step.step_kind == "list_dir":
            import os
            path = step.inputs.get("path", ".")
            try:
                items = os.listdir(path)
                actions.append(f"listed {path}: {len(items)} items")
                artifacts.append(path)
                summary = f"Directory {path}:\n" + "\n".join(items[:20])
                checks_passed.append(("dir_readable", True, f"{len(items)} items"))
            except Exception as e:
                summary = f"Failed to list {path}: {e}"
                checks_passed.append(("dir_readable", False, str(e)))

        else:
            summary = f"Unknown direct step kind: {step.step_kind}"
            checks_passed.append(("unknown", False, step.step_kind))

        return StatusPacket(
            step_id=step.step_id,
            objective=step.task_class,
            actions_taken=actions,
            artifacts_touched=artifacts,
            result_summary=summary,
            acceptance_checks=checks_passed,
            runner_id="direct",
            cost_units=0.0,
        )
