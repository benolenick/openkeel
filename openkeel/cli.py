"""OpenKeel CLI entry point.

Subcommands
-----------
init         Interactive first-run setup.
install      Generate hooks, wire into agent settings.
status       Show constitution + active mission.
constitution show|test  Display or test rules.
mission      start|show|update|plan|finding|end  Mission management.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("openkeel")


# ---------------------------------------------------------------------------
# Lazy imports
# ---------------------------------------------------------------------------


def _import_config():
    from openkeel.config import load_config, save_config, get_config_dir
    return load_config, save_config, get_config_dir


def _import_rules():
    from openkeel.constitution.rules import load_rules
    return load_rules


def _import_engine():
    from openkeel.constitution.engine import evaluate
    return evaluate


def _import_hooks():
    from openkeel.constitution.hooks import generate_enforce_hook
    return generate_enforce_hook


def _import_state():
    from openkeel.keel.state import (
        Mission, PlanStep, create_mission, save_mission, load_mission,
        list_missions, archive_mission, get_missions_dir, get_active_mission_name,
    )
    return (Mission, PlanStep, create_mission, save_mission, load_mission,
            list_missions, archive_mission, get_missions_dir, get_active_mission_name)


def _import_injector():
    from openkeel.keel.injector import generate_inject_hook
    return generate_inject_hook


def _import_drift():
    from openkeel.keel.drift import generate_drift_hook
    return generate_drift_hook


def _import_claude_adapter():
    from openkeel.adapters.claude import install_hooks, uninstall_hooks
    return install_hooks, uninstall_hooks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _yn(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        raw = input(f"{prompt} {hint}: ").strip().lower()
        if raw == "":
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Please enter y or n.")


def _prompt(prompt: str, default: str = "") -> str:
    if default:
        display = f"{prompt} [{default}]: "
    else:
        display = f"{prompt}: "
    value = input(display).strip()
    return value if value else default


def _print_section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def _set_active_mission(name: str) -> None:
    """Write the active mission name to the pointer file."""
    active_file = Path.home() / ".openkeel" / "active_mission.txt"
    active_file.parent.mkdir(parents=True, exist_ok=True)
    active_file.write_text(name, encoding="utf-8")


def _get_active_mission() -> str:
    """Read the active mission name from the pointer file."""
    active_file = Path.home() / ".openkeel" / "active_mission.txt"
    if active_file.exists():
        name = active_file.read_text(encoding="utf-8").strip()
        if name:
            return name
    load_config, _, _ = _import_config()
    config = load_config()
    return get_active_mission_name_from_config(config)


def get_active_mission_name_from_config(config: dict) -> str:
    return config.get("keel", {}).get("active_mission", "")


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> None:
    load_config, save_config, get_config_dir = _import_config()

    _print_section("OpenKeel — Interactive Setup")
    print("This wizard creates your OpenKeel configuration.")
    print("Press Enter to accept defaults.\n")

    config = load_config()
    config_dir = get_config_dir()

    # Constitution path
    print("-- Constitution --")
    const_path = _prompt(
        "Constitution rules file",
        default=config["constitution"]["path"],
    )
    config["constitution"]["path"] = const_path

    log_path = _prompt(
        "Enforcement log file",
        default=config["constitution"]["log_path"],
    )
    config["constitution"]["log_path"] = log_path

    # Keel (missions)
    print("\n-- Keel (Mission Persistence) --")
    missions_dir = _prompt(
        "Missions directory",
        default=config["keel"]["missions_dir"],
    )
    config["keel"]["missions_dir"] = missions_dir

    # Hooks output
    print("\n-- Hooks --")
    hooks_dir = _prompt(
        "Generated hooks directory",
        default=config["hooks"]["output_dir"],
    )
    config["hooks"]["output_dir"] = hooks_dir

    save_config(config)

    # Create constitution file from example if it doesn't exist
    const_file = Path(const_path).expanduser()
    if not const_file.exists():
        example = Path(__file__).parent.parent / "constitution.example.yaml"
        if example.exists():
            const_file.parent.mkdir(parents=True, exist_ok=True)
            const_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"\nCreated constitution from example: {const_file}")
        else:
            # Write a minimal constitution
            const_file.parent.mkdir(parents=True, exist_ok=True)
            const_file.write_text(
                "# OpenKeel Constitution\n# Add rules below.\n\nrules: []\n",
                encoding="utf-8",
            )
            print(f"\nCreated empty constitution: {const_file}")

    # Create missions directory
    Path(missions_dir).expanduser().mkdir(parents=True, exist_ok=True)

    _print_section("Setup Complete")
    print(f"\nConfig saved to: {config_dir / 'config.yaml'}")
    print(f"Constitution: {const_file}")
    print(f"Missions dir: {Path(missions_dir).expanduser()}")
    print("\nNext steps:")
    print("  1. Edit your constitution rules: openkeel constitution show")
    print("  2. Generate and install hooks: openkeel install")
    print("  3. Start a mission: openkeel mission start NAME --objective '...'")


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def cmd_install(args: argparse.Namespace) -> None:
    load_config, _, _ = _import_config()
    generate_enforce_hook = _import_hooks()
    generate_inject_hook = _import_injector()
    generate_drift_hook = _import_drift()
    install_hooks, _ = _import_claude_adapter()

    config = load_config()

    const_path = config["constitution"]["path"]
    log_path = config["constitution"]["log_path"]
    missions_dir = config["keel"]["missions_dir"]
    active_mission = _get_active_mission()
    hooks_dir = Path(config["hooks"]["output_dir"]).expanduser()
    hooks_dir.mkdir(parents=True, exist_ok=True)

    print("Generating hook scripts...")

    # Generate enforcement hook
    enforce_path = generate_enforce_hook(
        constitution_path=const_path,
        mission_dir=missions_dir,
        active_mission=active_mission,
        log_path=log_path,
        output_path=hooks_dir / "openkeel_enforce.py",
    )
    print(f"  Enforcement hook: {enforce_path}")

    # Generate injection hook
    inject_path = generate_inject_hook(
        missions_dir=missions_dir,
        active_mission=active_mission,
        output_path=hooks_dir / "openkeel_inject.py",
    )
    print(f"  Injection hook:   {inject_path}")

    # Generate drift hook
    drift_path = generate_drift_hook(
        missions_dir=missions_dir,
        output_path=hooks_dir / "openkeel_drift.py",
    )
    print(f"  Drift hook:       {drift_path}")

    # Wire into Claude Code
    print("\nInstalling hooks into Claude Code settings...")
    settings_path = install_hooks(enforce_path, inject_path, drift_path)
    print(f"  Settings updated: {settings_path}")

    print("\nDone. Hooks are active for new Claude Code sessions.")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> None:
    load_config, _, get_config_dir = _import_config()
    load_rules = _import_rules()

    config = load_config()
    config_dir = get_config_dir()

    _print_section("OpenKeel Status")

    # Config
    print(f"\nConfig file: {config_dir / 'config.yaml'}")

    # Constitution
    const_path = Path(config["constitution"]["path"]).expanduser()
    rules = load_rules(const_path) if const_path.exists() else []
    print(f"\nConstitution: {const_path}")
    print(f"  Rules loaded: {len(rules)}")
    if rules:
        deny_count = sum(1 for r in rules if r.action == "deny")
        alert_count = sum(1 for r in rules if r.action == "alert")
        print(f"  Deny rules: {deny_count}, Alert rules: {alert_count}")

    # Active mission
    active = _get_active_mission()
    if active:
        (_, _, _, _, load_mission, _, _, get_missions_dir, _) = _import_state()
        missions_dir = get_missions_dir(config)
        mission = load_mission(missions_dir, active)
        if mission:
            print(f"\nActive mission: {mission.name}")
            print(f"  Objective: {mission.objective}")
            done = sum(1 for s in mission.plan if s.status == "done")
            total = len(mission.plan)
            print(f"  Plan: {done}/{total} steps complete")
            print(f"  Findings: {len(mission.findings)}")
            if mission.tags:
                print(f"  Tags: {', '.join(mission.tags)}")
        else:
            print(f"\nActive mission: {active} (file not found)")
    else:
        print("\nActive mission: (none)")

    # Hooks
    hooks_dir = Path(config["hooks"]["output_dir"]).expanduser()
    hook_files = ["openkeel_enforce.py", "openkeel_inject.py", "openkeel_drift.py"]
    print(f"\nHooks directory: {hooks_dir}")
    for hf in hook_files:
        exists = (hooks_dir / hf).exists()
        marker = "[OK]" if exists else "[--]"
        print(f"  {marker} {hf}")

    print()


# ---------------------------------------------------------------------------
# constitution show / test
# ---------------------------------------------------------------------------


def cmd_constitution_show(args: argparse.Namespace) -> None:
    load_config, _, _ = _import_config()
    load_rules = _import_rules()

    config = load_config()
    const_path = Path(config["constitution"]["path"]).expanduser()

    if not const_path.exists():
        print(f"No constitution file at {const_path}")
        print("Run `openkeel init` to create one.")
        return

    rules = load_rules(const_path)
    if not rules:
        print("No rules defined in constitution.")
        return

    _print_section("Constitution Rules")
    for i, rule in enumerate(rules, 1):
        tags = f" [tags: {', '.join(rule.when_tags)}]" if rule.when_tags else ""
        print(f"\n  {i}. {rule.id}")
        print(f"     Tool: {rule.tool}")
        print(f"     Match: {rule.match.field} =~ /{rule.match.pattern}/")
        print(f"     Action: {rule.action}{tags}")
        if rule.message:
            print(f"     Message: {rule.message}")

    print()


def cmd_constitution_test(args: argparse.Namespace) -> None:
    load_config, _, _ = _import_config()
    load_rules = _import_rules()
    evaluate = _import_engine()

    config = load_config()
    const_path = Path(config["constitution"]["path"]).expanduser()

    if not const_path.exists():
        print(f"No constitution file at {const_path}")
        return

    rules = load_rules(const_path)
    command = args.command
    tool = args.tool

    # Build tool_input based on tool type
    if tool == "Bash":
        tool_input = {"command": command}
    elif tool in ("Write", "Edit", "Read"):
        tool_input = {"file_path": command}
    else:
        tool_input = {"command": command}

    # Get active tags
    active = _get_active_mission()
    active_tags = []
    if active:
        (_, _, _, _, load_mission, _, _, get_missions_dir, _) = _import_state()
        missions_dir = get_missions_dir(config)
        mission = load_mission(missions_dir, active)
        if mission:
            active_tags = mission.tags

    result = evaluate(rules, tool, tool_input, active_tags)

    if result.action == "deny":
        print(f"DENY — {result.message}")
        print(f"  Matched rule: {result.rule_id}")
    elif result.action == "alert":
        print(f"ALERT (allowed, but logged) — {result.message}")
        print(f"  Matched rule: {result.rule_id}")
    else:
        print("ALLOW — no rule matched")


# ---------------------------------------------------------------------------
# mission start/show/update/plan/finding/end
# ---------------------------------------------------------------------------


def cmd_mission_start(args: argparse.Namespace) -> None:
    load_config, save_config, _ = _import_config()
    (_, _, create_mission, _, _, _, _, get_missions_dir, _) = _import_state()

    config = load_config()
    name = args.name
    objective = args.objective or ""
    tags = args.tags.split(",") if args.tags else []

    mission = create_mission(config, name, objective=objective, tags=tags)
    _set_active_mission(name)

    print(f"Mission '{name}' created and set as active.")
    if objective:
        print(f"  Objective: {objective}")
    if tags:
        print(f"  Tags: {', '.join(tags)}")

    missions_dir = get_missions_dir(config)
    print(f"  File: {missions_dir / f'{name}.yaml'}")
    print("\nAdd plan steps with: openkeel mission plan add \"Step description\"")


def cmd_mission_show(args: argparse.Namespace) -> None:
    load_config, _, _ = _import_config()
    (_, _, _, _, load_mission, _, _, get_missions_dir, _) = _import_state()

    config = load_config()
    active = _get_active_mission()

    if not active:
        print("No active mission. Start one with: openkeel mission start NAME")
        return

    missions_dir = get_missions_dir(config)
    mission = load_mission(missions_dir, active)

    if not mission:
        print(f"Active mission '{active}' not found on disk.")
        return

    print(mission.format_injection())


def cmd_mission_update(args: argparse.Namespace) -> None:
    load_config, _, _ = _import_config()
    (_, _, _, save_mission, load_mission, _, _, get_missions_dir, _) = _import_state()

    config = load_config()
    active = _get_active_mission()

    if not active:
        print("No active mission.")
        return

    missions_dir = get_missions_dir(config)
    mission = load_mission(missions_dir, active)
    if not mission:
        print(f"Mission '{active}' not found.")
        return

    changed = False

    if args.objective:
        mission.objective = args.objective
        changed = True

    if args.notes:
        mission.notes = args.notes
        changed = True

    if args.add_tag:
        for tag in args.add_tag.split(","):
            tag = tag.strip()
            if tag and tag not in mission.tags:
                mission.tags.append(tag)
                changed = True

    if changed:
        save_mission(missions_dir, mission)
        print(f"Mission '{active}' updated.")
    else:
        print("Nothing to update. Use --objective, --notes, or --add-tag.")


def cmd_mission_plan_add(args: argparse.Namespace) -> None:
    load_config, _, _ = _import_config()
    (_, PlanStep, _, save_mission, load_mission, _, _, get_missions_dir, _) = _import_state()

    config = load_config()
    active = _get_active_mission()

    if not active:
        print("No active mission.")
        return

    missions_dir = get_missions_dir(config)
    mission = load_mission(missions_dir, active)
    if not mission:
        print(f"Mission '{active}' not found.")
        return

    next_id = max((s.id for s in mission.plan), default=0) + 1
    time_box = args.time_box if hasattr(args, "time_box") and args.time_box else 0

    step = PlanStep(id=next_id, step=args.step, time_box_minutes=time_box)
    mission.plan.append(step)
    save_mission(missions_dir, mission)

    print(f"Added step {next_id}: {args.step}")
    if time_box:
        print(f"  Time box: {time_box} minutes")


def cmd_mission_plan_status(args: argparse.Namespace) -> None:
    load_config, _, _ = _import_config()
    (_, _, _, save_mission, load_mission, _, _, get_missions_dir, _) = _import_state()

    config = load_config()
    active = _get_active_mission()

    if not active:
        print("No active mission.")
        return

    missions_dir = get_missions_dir(config)
    mission = load_mission(missions_dir, active)
    if not mission:
        print(f"Mission '{active}' not found.")
        return

    step_id = args.id
    new_status = args.status

    valid_statuses = ("pending", "in_progress", "done", "skipped")
    if new_status not in valid_statuses:
        print(f"Invalid status '{new_status}'. Must be one of: {', '.join(valid_statuses)}")
        return

    found = False
    for step in mission.plan:
        if step.id == step_id:
            step.status = new_status
            found = True
            break

    if not found:
        print(f"Step {step_id} not found in plan.")
        return

    save_mission(missions_dir, mission)
    print(f"Step {step_id} -> {new_status}")


def cmd_mission_finding_add(args: argparse.Namespace) -> None:
    load_config, _, _ = _import_config()
    (_, _, _, save_mission, load_mission, _, _, get_missions_dir, _) = _import_state()

    config = load_config()
    active = _get_active_mission()

    if not active:
        print("No active mission.")
        return

    missions_dir = get_missions_dir(config)
    mission = load_mission(missions_dir, active)
    if not mission:
        print(f"Mission '{active}' not found.")
        return

    mission.findings.append(args.text)
    save_mission(missions_dir, mission)
    print(f"Finding added ({len(mission.findings)} total)")


def cmd_mission_end(args: argparse.Namespace) -> None:
    load_config, _, _ = _import_config()
    (_, _, _, _, _, _, archive_mission, _, _) = _import_state()

    config = load_config()
    active = _get_active_mission()

    if not active:
        print("No active mission.")
        return

    if archive_mission(config, active):
        _set_active_mission("")
        print(f"Mission '{active}' archived and deactivated.")
    else:
        print(f"Mission '{active}' not found on disk.")


def cmd_mission_list(args: argparse.Namespace) -> None:
    load_config, _, _ = _import_config()
    (_, _, _, _, _, list_missions, _, get_missions_dir, _) = _import_state()

    config = load_config()
    missions_dir = get_missions_dir(config)
    missions = list_missions(missions_dir)
    active = _get_active_mission()

    if not missions:
        print("No missions found.")
        return

    print("Missions:")
    for name in missions:
        marker = " (active)" if name == active else ""
        print(f"  - {name}{marker}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="openkeel",
        description="OpenKeel — Immutable governance and mission persistence for CLI AI agents.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # init
    p_init = sub.add_parser("init", help="Interactive setup wizard.")
    p_init.set_defaults(func=cmd_init)

    # install
    p_install = sub.add_parser("install", help="Generate hooks, wire into agent settings.")
    p_install.set_defaults(func=cmd_install)

    # status
    p_status = sub.add_parser("status", help="Show constitution + active mission.")
    p_status.set_defaults(func=cmd_status)

    # constitution
    p_const = sub.add_parser("constitution", help="Constitution rule management.")
    const_sub = p_const.add_subparsers(dest="const_command", metavar="<subcommand>")
    const_sub.required = True

    p_const_show = const_sub.add_parser("show", help="Display all rules.")
    p_const_show.set_defaults(func=cmd_constitution_show)

    p_const_test = const_sub.add_parser("test", help="Dry-run a command against rules.")
    p_const_test.add_argument("command", help="The command string to test.")
    p_const_test.add_argument(
        "--tool", default="Bash",
        help="Tool name to simulate (default: Bash).",
    )
    p_const_test.set_defaults(func=cmd_constitution_test)

    # mission
    p_mission = sub.add_parser("mission", help="Mission state management.")
    mission_sub = p_mission.add_subparsers(dest="mission_command", metavar="<subcommand>")
    mission_sub.required = True

    # mission start
    p_ms = mission_sub.add_parser("start", help="Create and activate a mission.")
    p_ms.add_argument("name", help="Mission name (used as filename).")
    p_ms.add_argument("--objective", "-o", help="Mission objective.")
    p_ms.add_argument("--tags", "-t", help="Comma-separated tags (e.g. pentest,htb,windows).")
    p_ms.set_defaults(func=cmd_mission_start)

    # mission show
    p_mshow = mission_sub.add_parser("show", help="Display current mission state.")
    p_mshow.set_defaults(func=cmd_mission_show)

    # mission list
    p_mlist = mission_sub.add_parser("list", help="List all missions.")
    p_mlist.set_defaults(func=cmd_mission_list)

    # mission update
    p_mup = mission_sub.add_parser("update", help="Update mission fields.")
    p_mup.add_argument("--objective", help="New objective.")
    p_mup.add_argument("--notes", help="New notes.")
    p_mup.add_argument("--add-tag", help="Comma-separated tags to add.")
    p_mup.set_defaults(func=cmd_mission_update)

    # mission plan
    p_plan = mission_sub.add_parser("plan", help="Plan step management.")
    plan_sub = p_plan.add_subparsers(dest="plan_command", metavar="<subcommand>")
    plan_sub.required = True

    p_plan_add = plan_sub.add_parser("add", help="Add a plan step.")
    p_plan_add.add_argument("step", help="Step description.")
    p_plan_add.add_argument("--time-box", type=int, default=0, help="Time box in minutes.")
    p_plan_add.set_defaults(func=cmd_mission_plan_add)

    p_plan_status = plan_sub.add_parser("status", help="Set step status.")
    p_plan_status.add_argument("id", type=int, help="Step ID.")
    p_plan_status.add_argument("status", help="New status (pending/in_progress/done/skipped).")
    p_plan_status.set_defaults(func=cmd_mission_plan_status)

    # mission finding
    p_finding = mission_sub.add_parser("finding", help="Finding management.")
    finding_sub = p_finding.add_subparsers(dest="finding_command", metavar="<subcommand>")
    finding_sub.required = True

    p_finding_add = finding_sub.add_parser("add", help="Add a finding.")
    p_finding_add.add_argument("text", help="Finding text.")
    p_finding_add.set_defaults(func=cmd_mission_finding_add)

    # mission end
    p_mend = mission_sub.add_parser("end", help="Archive and deactivate the mission.")
    p_mend.set_defaults(func=cmd_mission_end)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
