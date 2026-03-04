"""OpenKeel CLI entry point.

Subcommands
-----------
init         Interactive first-run setup.
install      Generate hooks, wire into agent settings.
status       Show constitution + active mission.
constitution show|test  Display or test rules.
mission      start|show|update|plan|finding|end  Mission management.
timer        add|list|remove|clear  Self-timer management.
profile      list|show|validate  Profile management (full mode).
run          Launch an agent subprocess with proxy shell (full mode).
history      Query session history.
phase        next|show  Phase management (full mode).
journal      add|show|search|flush  Session journal.
wiki         add|show|list|categories|search|link|from-journal  Knowledge wiki.
task         add|show|edit|move|assign|delete|list|search|link|from-journal|stats  Task management.
board        [project] [--board]  Kanban board view.
serve        Start the embeddings server.
serve-status Check embeddings server status.
reindex      Rebuild all embeddings.
context      refresh  Context management.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
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


def _get_journal():
    from openkeel.integrations.journal import Journal
    return Journal()


def _get_wiki():
    from openkeel.integrations.wiki import Wiki
    return Wiki()


def _get_kanban():
    from openkeel.integrations.kanban import Kanban
    return Kanban()


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
# Timer helpers
# ---------------------------------------------------------------------------

TIMERS_PATH = Path.home() / ".openkeel" / "self_timers.json"


def _load_timers() -> list[dict]:
    if not TIMERS_PATH.exists():
        return []
    try:
        return json.loads(TIMERS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_timers(timers: list[dict]) -> None:
    TIMERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TIMERS_PATH.write_text(json.dumps(timers, indent=2), encoding="utf-8")


def _parse_duration(s: str) -> timedelta:
    """Parse a duration string like '5m', '1h', '2h30m', '90s'."""
    pattern = r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?'
    m = re.fullmatch(pattern, s.strip())
    if not m or not any(m.groups()):
        raise ValueError(f"Invalid duration: {s!r}. Use e.g. 5m, 1h, 2h30m, 90s")
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _slugify(text: str) -> str:
    """Turn a message into a short kebab-case ID."""
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return slug[:40]


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


def _resolve_fv_hooks_config(profile_name: str | None) -> dict:
    """Load a profile and resolve FV hooks config into hook-ready format.

    Resolves activity names (e.g. "exploitation") into regex pattern lists
    by looking up the profile's activities definitions.

    Returns a dict with keys matching generate_enforce_hook() FV params,
    or an empty dict (all disabled) if no profile or FV not enabled.
    """
    if not profile_name:
        return {}

    try:
        from openkeel.core.profile import load_profile
        profile = load_profile(profile_name)
    except (FileNotFoundError, ValueError) as exc:
        print(f"  Warning: could not load profile '{profile_name}': {exc}")
        return {}

    fv = profile.fv_hooks
    if not fv.enabled:
        return {}

    # Build activity name -> patterns lookup from profile
    activity_map: dict[str, list[str]] = {}
    for act in profile.activities:
        if act.name and act.patterns:
            activity_map[act.name] = act.patterns

    # Resolve mandatory/advisory activity names to regex pattern lists
    mandatory_patterns: list[str] = []
    for name in fv.mandatory_activities:
        if name in activity_map:
            mandatory_patterns.extend(activity_map[name])
        else:
            print(f"  Warning: mandatory activity '{name}' not found in profile activities")

    advisory_patterns: list[str] = []
    for name in fv.advisory_activities:
        if name in activity_map:
            advisory_patterns.extend(activity_map[name])
        else:
            print(f"  Warning: advisory activity '{name}' not found in profile activities")

    return {
        "fv_enabled": True,
        "fv_endpoint": fv.endpoint,
        "fv_timeout": fv.timeout,
        "fv_top_k": fv.top_k,
        "fv_mandatory_patterns": mandatory_patterns,
        "fv_advisory_patterns": advisory_patterns,
        "fv_tool_queries": fv.tool_queries,
    }


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

    # Resolve FV hooks config from profile (if specified)
    profile_name = getattr(args, "profile", None)
    fv_config = _resolve_fv_hooks_config(profile_name)

    print("Generating hook scripts...")
    if fv_config:
        print(f"  FV memory enforcement: ENABLED (profile: {profile_name})")
        print(f"    Endpoint: {fv_config['fv_endpoint']}")
        print(f"    Mandatory patterns: {len(fv_config['fv_mandatory_patterns'])}")
        print(f"    Advisory patterns: {len(fv_config['fv_advisory_patterns'])}")
        print(f"    Tool queries: {len(fv_config['fv_tool_queries'])}")

    # Generate enforcement hook
    enforce_path = generate_enforce_hook(
        constitution_path=const_path,
        mission_dir=missions_dir,
        active_mission=active_mission,
        log_path=log_path,
        output_path=hooks_dir / "openkeel_enforce.py",
        **fv_config,
    )
    print(f"  Enforcement hook: {enforce_path}")

    # Generate injection hook (with FV health check if enabled)
    inject_fv_kwargs = {}
    if fv_config:
        inject_fv_kwargs = {
            "fv_enabled": True,
            "fv_endpoint": fv_config["fv_endpoint"],
        }
    inject_path = generate_inject_hook(
        missions_dir=missions_dir,
        active_mission=active_mission,
        output_path=hooks_dir / "openkeel_inject.py",
        **inject_fv_kwargs,
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
# profile list / show / validate
# ---------------------------------------------------------------------------


def cmd_profile_list(args: argparse.Namespace) -> None:
    from openkeel.core.profile import list_profiles
    profiles = list_profiles()
    if not profiles:
        print("No profiles found.")
        return
    print("Available profiles:")
    for name in profiles:
        print(f"  - {name}")


def cmd_profile_show(args: argparse.Namespace) -> None:
    from openkeel.core.profile import load_profile
    try:
        profile = load_profile(args.name)
    except FileNotFoundError as exc:
        print(str(exc))
        return

    _print_section(f"Profile: {profile.name}")
    if profile.description:
        print(f"\n  {profile.description}")
    print(f"\n  Version: {profile.version}")
    print(f"  Default action: {profile.default_action}")
    if profile.tags:
        print(f"  Tags: {', '.join(profile.tags)}")

    print(f"\n  BLOCKED patterns: {len(profile.blocked.patterns)}")
    if profile.blocked.message:
        print(f"    Message: {profile.blocked.message}")

    print(f"  GATED patterns: {len(profile.gated.patterns)}")
    if profile.gated.message:
        print(f"    Message: {profile.gated.message}")

    print(f"  SAFE patterns: {len(profile.safe.patterns)}")
    if profile.safe.message:
        print(f"    Message: {profile.safe.message}")

    if profile.scope.allowed_ips:
        print(f"\n  Scope — allowed IPs: {', '.join(profile.scope.allowed_ips)}")
    if profile.scope.allowed_hostnames:
        print(f"  Scope — allowed hostnames: {', '.join(profile.scope.allowed_hostnames)}")
    if profile.scope.denied_paths:
        print(f"  Scope — denied paths: {len(profile.scope.denied_paths)}")

    if profile.activities:
        print(f"\n  Activities ({len(profile.activities)}):")
        for a in profile.activities:
            tb = f" (timebox: {a.timebox_minutes}min)" if a.timebox_minutes else ""
            print(f"    - {a.name}: {len(a.patterns)} patterns{tb}")

    if profile.phases:
        print(f"\n  Phases ({len(profile.phases)}):")
        for p in profile.phases:
            to = f" (timeout: {p.timeout_minutes}min)" if p.timeout_minutes else ""
            gates = f", {len(p.gates)} gates" if p.gates else ""
            print(f"    - {p.name}{to}{gates}")

    print(f"\n  Re-injection: capsule every {profile.reinjection.capsule_every}, full every {profile.reinjection.full_every}")
    print(f"  Sandbox: {'enabled' if profile.sandbox.enabled else 'disabled'}")
    print()


def cmd_profile_validate(args: argparse.Namespace) -> None:
    from openkeel.core.profile import load_profile, validate_profile
    try:
        profile = load_profile(args.file)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return

    issues = validate_profile(profile)
    if not issues:
        print(f"Profile '{profile.name}' is valid.")
    else:
        print(f"Profile '{profile.name}' has {len(issues)} issue(s):")
        for issue in issues:
            print(f"  - {issue}")


# ---------------------------------------------------------------------------
# run (full mode)
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> None:
    import subprocess as sp
    from openkeel.core.profile import load_profile, validate_profile
    from openkeel.core.history import get_connection, start_session, end_session, sync_jsonl_to_db
    from openkeel.core.sandbox import is_available as sandbox_available, build_systemd_run_args
    from openkeel.core.gates import advance_phase

    profile_name = args.profile
    agent_cmd = args.agent_command
    # Strip leading -- if present (argparse REMAINDER includes it)
    if agent_cmd and agent_cmd[0] == "--":
        agent_cmd = agent_cmd[1:]

    if not agent_cmd:
        print("ERROR: No agent command specified.")
        print("Usage: openkeel run --profile NAME -- agent-command args...")
        sys.exit(1)

    # Load and validate profile
    try:
        profile = load_profile(profile_name)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    issues = validate_profile(profile)
    if issues:
        print(f"WARNING: Profile '{profile.name}' has issues:")
        for issue in issues:
            print(f"  - {issue}")

    # Project = profile name (unified concept)
    project = profile.name

    # Generate session ID
    session_id = str(uuid.uuid4())[:12]

    # Set up session directories
    session_dir = Path.home() / ".openkeel" / "sessions" / session_id
    log_dir = session_dir / "logs"
    state_dir = session_dir / "state"
    log_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    # Save session metadata
    import json
    meta = {"profile": profile_name, "project": project, "session_id": session_id}
    (state_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    # Find the openkeel-exec shell
    exec_path = shutil.which("openkeel-exec")
    if not exec_path:
        # Fall back to running as module
        exec_path = f"{sys.executable} -m openkeel.exec"

    print(f"OpenKeel Full Mode")
    print(f"  Session:  {session_id}")
    print(f"  Profile:  {profile.name}")
    if project:
        print(f"  Project:  {project}")
    print(f"  Agent:    {' '.join(agent_cmd)}")
    print(f"  Log dir:  {log_dir}")
    print()

    # Record session in history DB
    conn = get_connection()
    start_session(conn, session_id, project=project, profile=profile.name)

    # Auto-advance to first phase if phases defined
    if profile.phases:
        phase_state = state_dir / "phase.json"
        advance_phase(profile, phase_state, str(log_dir / "session.jsonl"), session_id, force=True)
        print(f"  Phase:    {profile.phases[0].name}")
        print()

    # Build environment
    env = os.environ.copy()
    env["OPENKEEL_EXEC"] = exec_path  # for autopwn CommandRunner integration
    env["CLAUDE_CODE_SHELL"] = exec_path  # Claude Code ignores $SHELL, uses this instead
    env["OPENKEEL_PROFILE"] = profile_name
    env["OPENKEEL_SESSION_ID"] = session_id
    env["OPENKEEL_LOG_DIR"] = str(log_dir)
    # Preserve the real shell for exec.py to use
    if "SHELL" in os.environ:
        env["OPENKEEL_REAL_SHELL"] = os.environ["SHELL"]
    # Only override SHELL on Linux — on Windows, MSYS2/Git Bash uses SHELL
    # internally for fork() and setting it to a non-bash path causes crashes
    if sys.platform != "win32":
        env["SHELL"] = exec_path

    # Build command with optional sandbox
    cmd = list(agent_cmd)
    if profile.sandbox.enabled and sandbox_available():
        sandbox_args = build_systemd_run_args(profile.sandbox, f"openkeel-{session_id}")
        cmd = sandbox_args + cmd
        print("  Sandbox:  enabled (systemd-run)")

    # Start timer manager
    from openkeel.core.timers import TimerManager
    timer_mgr = TimerManager(
        timers=profile.timers,
        log_path=str(log_dir / "session.jsonl"),
        session_id=session_id,
        state_dir=str(state_dir),
    )
    timer_mgr.start()

    # Launch agent subprocess
    exit_code = 0
    try:
        proc = sp.Popen(cmd, env=env)
        exit_code = proc.wait()
    except KeyboardInterrupt:
        print("\n[openkeel] Session interrupted.")
        exit_code = 130
    except FileNotFoundError:
        print(f"ERROR: Agent command not found: {agent_cmd[0]}")
        exit_code = 127
    finally:
        # Stop timer manager
        timer_mgr.stop()

        # End session in DB
        status = "completed" if exit_code == 0 else "interrupted" if exit_code == 130 else "failed"
        end_session(conn, session_id, status=status)

        # Batch import JSONL to SQLite
        jsonl_path = log_dir / "session.jsonl"
        if jsonl_path.exists():
            imported = sync_jsonl_to_db(conn, session_id, jsonl_path)
            print(f"\n[openkeel] Session {session_id} ended. {imported} events recorded.")

        conn.close()

        # Post-session learning: extract lessons and seed to memory backend
        if profile.learning.enabled and jsonl_path.exists():
            try:
                from openkeel.core.learning import run_post_session_learning
                learned = run_post_session_learning(
                    log_path=jsonl_path,
                    config=profile.learning,
                    project=project,
                    profile_name=profile.name,
                    session_id=session_id,
                )
                if learned:
                    print(f"[openkeel] Learning: {learned} lessons seeded to memory backend.")
            except Exception as exc:
                logger.warning("Post-session learning failed: %s", exc)

    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def cmd_history(args: argparse.Namespace) -> None:
    from openkeel.core.history import (
        get_connection, query_sessions, search_events,
        get_session_events, get_session_phases, get_stats,
    )

    conn = get_connection()

    if args.stats:
        stats = get_stats(conn)
        _print_section("Session Statistics")
        print(f"\n  Total sessions: {stats['total_sessions']}")
        print(f"  Total events:   {stats['total_events']}")
        print(f"  Total blocked:  {stats['total_blocked']}")
        if stats["by_status"]:
            print("\n  By status:")
            for status, count in stats["by_status"].items():
                print(f"    {status}: {count}")
        if stats["by_project"]:
            print("\n  By project:")
            for project, count in stats["by_project"].items():
                print(f"    {project}: {count}")
        if stats["top_blocked"]:
            print("\n  Top blocked commands:")
            for item in stats["top_blocked"][:5]:
                print(f"    [{item['count']}x] {item['command'][:60]}")
        print()
        conn.close()
        return

    if args.session:
        # Show details for a specific session
        events = get_session_events(conn, args.session)
        phases = get_session_phases(conn, args.session)

        _print_section(f"Session: {args.session}")
        if phases:
            print("\n  Phases:")
            for p in phases:
                exit_info = f" -> {p['exited_at']}" if p.get("exited_at") else " (current)"
                print(f"    {p['phase_name']}: {p['entered_at']}{exit_info}")

        if events:
            print(f"\n  Events ({len(events)}):")
            for e in events[-20:]:  # Show last 20
                action_marker = "DENY" if e["action"] == "deny" else "OK"
                activity = f" [{e['activity']}]" if e.get("activity") else ""
                cmd = e["command"][:50] if e.get("command") else ""
                print(f"    [{action_marker}]{activity} {cmd}")
            if len(events) > 20:
                print(f"    ... and {len(events) - 20} more events")
        else:
            print("\n  No events recorded.")
        print()
        conn.close()
        return

    if args.search:
        events = search_events(conn, args.search, limit=20)
        if not events:
            print(f"No events matching '{args.search}'.")
        else:
            print(f"Events matching '{args.search}':")
            for e in events:
                action_marker = "DENY" if e["action"] == "deny" else "OK"
                print(f"  [{e['session_id'][:8]}] [{action_marker}] {e['command'][:60]}")
        conn.close()
        return

    # Default: list recent sessions
    sessions = query_sessions(conn, project=args.project, status=args.status, limit=20)
    if not sessions:
        print("No sessions found.")
        conn.close()
        return

    print("Recent sessions:")
    for s in sessions:
        status_marker = {"running": ">", "completed": ".", "failed": "!", "interrupted": "x"}.get(s["status"], "?")
        project_info = f" [{s['project']}]" if s.get("project") else ""
        profile_info = f" ({s['profile']})" if s.get("profile") else ""
        print(f"  [{status_marker}] {s['id']}{project_info}{profile_info}  cmds={s['command_count']} blocked={s['blocked_count']}  {s['started_at'][:19]}")

    conn.close()


# ---------------------------------------------------------------------------
# phase next / show
# ---------------------------------------------------------------------------


def cmd_phase_show(args: argparse.Namespace) -> None:
    from openkeel.core.profile import load_profile
    from openkeel.core.gates import get_current_phase, check_phase_timeout, _load_phase_state

    session_dir = _find_session_dir(args.session)
    if not session_dir:
        return

    profile_name = _read_session_profile(session_dir)
    if not profile_name:
        print("Could not determine session profile.")
        return

    profile = load_profile(profile_name)
    state_path = session_dir / "state" / "phase.json"

    current = get_current_phase(profile, state_path)
    if not current:
        print("No active phase.")
        return

    state = _load_phase_state(state_path)
    timed_out, remaining = check_phase_timeout(profile, state_path)

    _print_section("Phase Status")
    print(f"\n  Current phase: {current.name}")
    if current.description:
        print(f"  Description: {current.description}")
    if current.timeout_minutes:
        status = "TIMED OUT" if timed_out else f"{remaining:.0f}min remaining"
        print(f"  Timeout: {current.timeout_minutes}min ({status})")
    if current.gates:
        print(f"  Gates: {len(current.gates)}")

    idx = state.get("current_index", 0)
    total = len(profile.phases)
    print(f"\n  Progress: phase {idx + 1}/{total}")
    for i, phase in enumerate(profile.phases):
        if i < idx:
            marker = "[x]"
        elif i == idx:
            marker = "[>]"
        else:
            marker = "[ ]"
        print(f"    {marker} {phase.name}")

    if state.get("history"):
        print(f"\n  Completed phases: {len(state['history'])}")

    print()


def cmd_phase_next(args: argparse.Namespace) -> None:
    from openkeel.core.profile import load_profile
    from openkeel.core.gates import advance_phase

    session_dir = _find_session_dir(args.session)
    if not session_dir:
        return

    profile_name = _read_session_profile(session_dir)
    if not profile_name:
        print("Could not determine session profile.")
        return

    profile = load_profile(profile_name)
    state_path = session_dir / "state" / "phase.json"
    log_path = session_dir / "logs" / "session.jsonl"

    force = getattr(args, "force", False)
    success, message = advance_phase(profile, state_path, log_path, args.session, force=force)

    if success:
        print(f"OK: {message}")
    else:
        print(f"FAILED: {message}")
        if not force:
            print("  Use --force to skip gate checks.")


def _find_session_dir(session_id: str) -> Path | None:
    """Find a session directory by ID (full or prefix)."""
    sessions_base = Path.home() / ".openkeel" / "sessions"
    if not sessions_base.exists():
        print("No sessions found.")
        return None

    # Try exact match
    exact = sessions_base / session_id
    if exact.exists():
        return exact

    # Try prefix match
    candidates = [d for d in sessions_base.iterdir() if d.is_dir() and d.name.startswith(session_id)]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        print(f"Ambiguous session ID '{session_id}'. Matches: {', '.join(c.name for c in candidates)}")
        return None

    print(f"Session '{session_id}' not found.")
    return None


def _read_session_profile(session_dir: Path) -> str:
    """Read the profile name from a session's env (stored by runner)."""
    # Check if there's a metadata file
    meta_path = session_dir / "state" / "meta.json"
    if meta_path.exists():
        import json
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return meta.get("profile", "")
        except (json.JSONDecodeError, OSError):
            pass

    # Fall back to scanning JSONL for profile info
    jsonl_path = session_dir / "logs" / "session.jsonl"
    if jsonl_path.exists():
        import json
        try:
            first_line = jsonl_path.read_text(encoding="utf-8").split("\n")[0]
            record = json.loads(first_line)
            return record.get("profile", "")
        except (json.JSONDecodeError, OSError, IndexError):
            pass

    return ""


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------


def cmd_launch(args: argparse.Namespace) -> None:
    from openkeel.launch import launch
    launch(
        agent_name=getattr(args, "agent", "") or "",
    )


# ---------------------------------------------------------------------------
# remember / recall / memory
# ---------------------------------------------------------------------------


def _get_memory():
    from openkeel.integrations.local_memory import LocalMemory
    return LocalMemory()


def cmd_remember(args: argparse.Namespace) -> None:
    mem = _get_memory()
    fact_id = mem.remember(
        args.fact,
        project=args.project,
        tag=args.tag,
        source=args.source,
    )
    print(f"Remembered #{fact_id}: {args.fact[:80]}")
    if args.project:
        print(f"  project: {args.project}")
    if args.tag:
        print(f"  tag: {args.tag}")
    mem.close()


def cmd_recall(args: argparse.Namespace) -> None:
    mem = _get_memory()
    results = mem.recall(args.query, top_k=args.top, project=args.project)
    if not results:
        print(f"No results for: {args.query}")
        mem.close()
        return
    print(f"Results for: {args.query}\n")
    for r in results:
        project_tag = f" [{r['project']}]" if r['project'] else ""
        tag_str = f" #{r['tag']}" if r['tag'] else ""
        print(f"  #{r['id']} (score: {r['score']:.2f}){project_tag}{tag_str}")
        print(f"    {r['text'][:120]}")
        print()
    mem.close()


def cmd_memory_stats(args: argparse.Namespace) -> None:
    mem = _get_memory()
    s = mem.stats()
    print(f"Memory: {s['total_facts']} facts in {s['db_path']} ({s['db_size_kb']} KB)")
    if s['projects']:
        print("\n  Projects:")
        for p, cnt in s['projects'].items():
            print(f"    {p}: {cnt}")
    if s['tags']:
        print("\n  Tags:")
        for t, cnt in s['tags'].items():
            print(f"    {t}: {cnt}")
    mem.close()


def cmd_memory_recent(args: argparse.Namespace) -> None:
    mem = _get_memory()
    results = mem.recent(limit=args.limit, project=args.project)
    if not results:
        print("No facts stored yet.")
        mem.close()
        return
    import datetime
    for r in results:
        ts = datetime.datetime.fromtimestamp(r['created_at']).strftime('%Y-%m-%d %H:%M')
        project_tag = f" [{r['project']}]" if r['project'] else ""
        tag_str = f" #{r['tag']}" if r['tag'] else ""
        print(f"  #{r['id']} {ts}{project_tag}{tag_str}")
        print(f"    {r['text'][:120]}")
        print()
    mem.close()


def cmd_memory_export(args: argparse.Namespace) -> None:
    mem = _get_memory()
    print(mem.export_jsonl())
    mem.close()


def cmd_memory_delete(args: argparse.Namespace) -> None:
    mem = _get_memory()
    mem.delete(args.id)
    print(f"Deleted fact #{args.id}")
    mem.close()


# ---------------------------------------------------------------------------
# Timer commands
# ---------------------------------------------------------------------------


def cmd_timer_add(args: argparse.Namespace) -> None:
    """Add a self-timer."""
    duration = _parse_duration(args.in_duration)
    now = datetime.now(timezone.utc)
    fire_at = now + duration

    timer_id = _slugify(args.message)
    timers = _load_timers()

    # Deduplicate by ID
    timers = [t for t in timers if t.get("id") != timer_id]

    timer = {
        "id": timer_id,
        "message": args.message,
        "fire_at": fire_at.isoformat(),
        "created_at": now.isoformat(),
        "repeat_minutes": int(duration.total_seconds() // 60) if args.repeat else None,
        "fired": False,
    }
    timers.append(timer)
    _save_timers(timers)

    repeat_note = f" (repeats every {timer['repeat_minutes']}m)" if args.repeat else ""
    print(f"Timer set: \"{args.message}\" fires in {args.in_duration}{repeat_note}")
    print(f"  ID: {timer_id}")
    print(f"  Fire at: {fire_at.strftime('%H:%M:%S UTC')}")


def cmd_timer_list(args: argparse.Namespace) -> None:
    """List active timers."""
    timers = _load_timers()
    active = [t for t in timers if not t.get("fired")]
    if not active:
        print("No active timers.")
        return

    now = datetime.now(timezone.utc)
    print(f"{'ID':<30} {'Message':<35} {'Fires in':<15} {'Repeat'}")
    print("-" * 90)
    for t in active:
        fire_at = datetime.fromisoformat(t["fire_at"])
        delta = fire_at - now
        if delta.total_seconds() <= 0:
            remaining = "OVERDUE"
        else:
            mins, secs = divmod(int(delta.total_seconds()), 60)
            hours, mins = divmod(mins, 60)
            if hours:
                remaining = f"{hours}h {mins}m"
            elif mins:
                remaining = f"{mins}m {secs}s"
            else:
                remaining = f"{secs}s"

        repeat = f"every {t['repeat_minutes']}m" if t.get("repeat_minutes") else "-"
        print(f"{t['id']:<30} {t['message']:<35} {remaining:<15} {repeat}")


def cmd_timer_remove(args: argparse.Namespace) -> None:
    """Remove a timer by ID."""
    timers = _load_timers()
    before = len(timers)
    timers = [t for t in timers if t.get("id") != args.timer_id]
    if len(timers) == before:
        print(f"No timer found with ID: {args.timer_id}")
        return
    _save_timers(timers)
    print(f"Removed timer: {args.timer_id}")


def cmd_timer_clear(args: argparse.Namespace) -> None:
    """Remove all timers."""
    _save_timers([])
    print("All timers cleared.")


# ---------------------------------------------------------------------------
# Journal commands
# ---------------------------------------------------------------------------


def cmd_journal_add(args: argparse.Namespace) -> None:
    journal = _get_journal()
    entry_id = journal.add_entry(
        body=args.body,
        title=getattr(args, 'title', '') or '',
        project=args.project,
        entry_type=args.entry_type,
        tags=args.tags,
        session_id=getattr(args, 'session_id', '') or '',
        mission_name=getattr(args, 'mission_name', '') or '',
    )
    print(f"Journal entry #{entry_id} added.")
    if args.project:
        print(f"  project: {args.project}")
    journal.close()


def cmd_journal_show(args: argparse.Namespace) -> None:
    journal = _get_journal()
    entries = journal.get_entries(
        project=args.project,
        limit=args.limit,
        entry_type=args.entry_type,
    )
    if not entries:
        print("No journal entries found.")
        journal.close()
        return
    import datetime as dt
    for e in entries:
        ts = dt.datetime.fromtimestamp(e['timestamp']).strftime('%Y-%m-%d %H:%M')
        title = e.get('title') or '(untitled)'
        project_tag = f" [{e['project']}]" if e.get('project') else ""
        etype = f" ({e['entry_type']})" if e.get('entry_type') else ""
        print(f"  #{e['id']} {ts}{project_tag}{etype}")
        print(f"    {title}")
        body_preview = (e.get('body') or '')[:120]
        if body_preview:
            print(f"    {body_preview}")
        print()
    journal.close()


def cmd_journal_search(args: argparse.Namespace) -> None:
    journal = _get_journal()
    if args.semantic:
        results = journal.search_semantic(args.query, top_k=args.top, project=args.project)
    else:
        results = journal.search_keyword(args.query, top_k=args.top, project=args.project)
    if not results:
        print(f"No results for: {args.query}")
        journal.close()
        return
    mode = "semantic" if args.semantic else "keyword"
    print(f"Journal search ({mode}): {args.query}\n")
    for r in results:
        score = r.get('score', 0)
        project_tag = f" [{r.get('project', '')}]" if r.get('project') else ""
        title = r.get('title', '') or '(untitled)'
        print(f"  #{r.get('id', '?')} (score: {score:.2f}){project_tag}")
        print(f"    {title}")
        body = (r.get('body') or r.get('text_preview') or '')[:120]
        if body:
            print(f"    {body}")
        print()
    journal.close()


def cmd_journal_flush(args: argparse.Namespace) -> None:
    """Read enforcement.log, extract session summary, auto-promote decisions to wiki."""
    from openkeel.config import load_config
    config = load_config()
    log_path = Path(config["constitution"]["log_path"]).expanduser()

    if not log_path.exists():
        print(f"No enforcement log at {log_path}")
        return

    # Read recent log entries
    entries = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError as exc:
        print(f"Error reading log: {exc}")
        return

    if not entries:
        print("No entries in enforcement log.")
        return

    # Group by action
    denies = [e for e in entries if e.get("action") == "deny"]
    alerts = [e for e in entries if e.get("action") == "alert"]
    allows = len(entries) - len(denies) - len(alerts)

    # Build summary
    accomplishments = [f"Processed {len(entries)} tool calls ({allows} allowed, {len(denies)} denied, {len(alerts)} alerts)"]
    decisions = []
    blockers = []

    if denies:
        unique_rules = set(e.get("rule_id", "unknown") for e in denies)
        decisions.append(f"Blocked {len(denies)} calls via rules: {', '.join(unique_rules)}")
        for d in denies[:3]:
            blockers.append(f"Denied: {d.get('message', 'unknown')[:80]}")

    journal = _get_journal()
    session_id = args.session_id if hasattr(args, 'session_id') and args.session_id else ""
    project = args.project if hasattr(args, 'project') and args.project else ""

    entry_id = journal.add_session_summary(
        session_id=session_id,
        project=project,
        accomplishments=accomplishments,
        decisions=decisions,
        blockers=blockers,
    )
    print(f"Flushed enforcement log to journal entry #{entry_id}")
    print(f"  {len(entries)} events summarized")
    if decisions:
        print(f"  {len(decisions)} decisions promoted to wiki")
    journal.close()


# ---------------------------------------------------------------------------
# Wiki commands
# ---------------------------------------------------------------------------


def cmd_wiki_add(args: argparse.Namespace) -> None:
    wiki = _get_wiki()
    page_id = wiki.add_page(
        title=args.title,
        body=args.body,
        category=args.category,
        project=args.project,
        tags=args.tags,
    )
    print(f"Wiki page #{page_id}: {args.title}")
    if args.category:
        print(f"  category: {args.category}")
    wiki.close()


def cmd_wiki_show(args: argparse.Namespace) -> None:
    wiki = _get_wiki()
    page = wiki.get_page(args.slug)
    if not page:
        print(f"No wiki page with slug: {args.slug}")
        wiki.close()
        return
    _print_section(page.get('title', args.slug))
    if page.get('category'):
        print(f"Category: {page['category']}")
    if page.get('project'):
        print(f"Project: {page['project']}")
    if page.get('tags'):
        print(f"Tags: {page['tags']}")
    print()
    print(page.get('body', ''))
    links = page.get('links', [])
    if links:
        print(f"\nLinked pages: {', '.join(links)}")
    wiki.close()


def cmd_wiki_list(args: argparse.Namespace) -> None:
    wiki = _get_wiki()
    pages = wiki.list_pages(
        category=getattr(args, 'category', '') or '',
        project=getattr(args, 'project', '') or '',
    )
    if not pages:
        print("No wiki pages found.")
        wiki.close()
        return
    import datetime as dt
    for p in pages:
        ts = dt.datetime.fromtimestamp(p['updated_at']).strftime('%Y-%m-%d %H:%M')
        cat = f" [{p['category']}]" if p.get('category') else ""
        proj = f" ({p['project']})" if p.get('project') else ""
        print(f"  {p['slug']}{cat}{proj} — {ts}")
    wiki.close()


def cmd_wiki_categories(args: argparse.Namespace) -> None:
    wiki = _get_wiki()
    cats = wiki.list_categories()
    if not cats:
        print("No categories found.")
        wiki.close()
        return
    print("Wiki categories:\n")
    for c in cats:
        print(f"  {c['category']}: {c['count']} pages")
    wiki.close()


def cmd_wiki_search(args: argparse.Namespace) -> None:
    wiki = _get_wiki()
    if args.semantic:
        results = wiki.search_semantic(args.query, top_k=args.top)
    else:
        results = wiki.search_keyword(args.query, top_k=args.top)
    if not results:
        print(f"No results for: {args.query}")
        wiki.close()
        return
    mode = "semantic" if args.semantic else "keyword"
    print(f"Wiki search ({mode}): {args.query}\n")
    for r in results:
        score = r.get('score', 0)
        slug = r.get('slug', '?')
        title = r.get('title', slug)
        print(f"  {slug} (score: {score:.2f})")
        print(f"    {title}")
        body = (r.get('body') or r.get('text_preview') or '')[:120]
        if body:
            print(f"    {body}")
        print()
    wiki.close()


def cmd_wiki_link(args: argparse.Namespace) -> None:
    wiki = _get_wiki()
    ok = wiki.link_pages(args.from_slug, args.to_slug)
    if ok:
        print(f"Linked: {args.from_slug} -> {args.to_slug}")
    else:
        print(f"Failed to link. Check that both slugs exist.")
    wiki.close()


def cmd_wiki_from_journal(args: argparse.Namespace) -> None:
    wiki = _get_wiki()
    try:
        page_id = wiki.from_journal(
            journal_id=args.journal_id,
            title=getattr(args, 'title', '') or '',
            category=getattr(args, 'category', '') or '',
        )
        print(f"Created wiki page #{page_id} from journal entry #{args.journal_id}")
    except ValueError as exc:
        print(f"Error: {exc}")
    wiki.close()


# ---------------------------------------------------------------------------
# Serve commands (embeddings server)
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the embeddings server."""
    from openkeel.integrations.embeddings_server import run_server
    port = getattr(args, 'port', 7437) or 7437
    print(f"Starting embeddings server on port {port}...")
    run_server(port=port)


def cmd_serve_status(args: argparse.Namespace) -> None:
    """Check embeddings server status."""
    from openkeel.integrations.embeddings_client import EmbeddingsClient
    client = EmbeddingsClient()
    if client.is_available():
        print("Embeddings server: RUNNING")
    else:
        print("Embeddings server: NOT RUNNING")
        print("  Start with: openkeel serve")


def cmd_reindex(args: argparse.Namespace) -> None:
    """Rebuild all embeddings."""
    from openkeel.integrations.embeddings_client import EmbeddingsClient
    client = EmbeddingsClient()
    if not client.is_available():
        print("Embeddings server is not running. Start with: openkeel serve")
        return
    print("Reindexing all journal and wiki entries...")
    ok = client.reindex()
    if ok:
        print("Reindex complete.")
    else:
        print("Reindex failed.")


# ---------------------------------------------------------------------------
# Task / Kanban commands
# ---------------------------------------------------------------------------


def cmd_task_add(args: argparse.Namespace) -> None:
    """Create a new task."""
    kanban = _get_kanban()
    due = None
    if getattr(args, "due", None):
        try:
            due = datetime.strptime(args.due, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            print(f"Invalid date format: {args.due} (expected YYYY-MM-DD)")
            kanban.close()
            return

    task_id = kanban.add_task(
        title=args.title,
        description=getattr(args, "desc", "") or "",
        priority=getattr(args, "priority", "medium") or "medium",
        type=getattr(args, "type", "task") or "task",
        project=getattr(args, "project", "") or "",
        tags=getattr(args, "tags", "") or "",
        assigned_to=getattr(args, "assign", "") or "",
        board=getattr(args, "board", "default") or "default",
        due_date=due,
        parent_id=getattr(args, "parent", None),
    )
    kanban.close()
    print(f"Task #{task_id} created: {args.title}")


def cmd_task_show(args: argparse.Namespace) -> None:
    """Show full task details."""
    kanban = _get_kanban()
    task = kanban.get_task(args.id)
    kanban.close()

    if not task:
        print(f"Task #{args.id} not found.")
        return

    ts_created = datetime.fromtimestamp(task["created_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    ts_updated = datetime.fromtimestamp(task["updated_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

    print(f"Task #{task['id']}: {task['title']}")
    print(f"  Status:   {task['status']}")
    print(f"  Priority: {task['priority']}")
    print(f"  Type:     {task['type']}")
    if task["project"]:
        print(f"  Project:  {task['project']}")
    if task["board"] != "default":
        print(f"  Board:    {task['board']}")
    if task["assigned_to"]:
        print(f"  Assigned: {task['assigned_to']}")
    if task["tags"]:
        print(f"  Tags:     {task['tags']}")
    if task["due_date"]:
        due = datetime.fromtimestamp(task["due_date"], tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"  Due:      {due}")
    if task["parent_id"]:
        print(f"  Parent:   #{task['parent_id']}")
    print(f"  Created:  {ts_created}")
    print(f"  Updated:  {ts_updated}")

    if task.get("description"):
        print(f"\n{task['description']}")

    subtasks = task.get("subtasks", [])
    if subtasks:
        print(f"\nSubtasks ({len(subtasks)}):")
        for st in subtasks:
            marker = "x" if st["status"] == "done" else " "
            print(f"  [{marker}] #{st['id']} {st['title']} ({st['status']})")

    wiki_links = task.get("wiki_links", [])
    if wiki_links:
        print(f"\nLinked wiki pages ({len(wiki_links)}):")
        for wl in wiki_links:
            cat = f" ({wl['category']})" if wl.get("category") else ""
            print(f"  - {wl['slug']}: {wl['title']}{cat}")


def cmd_task_edit(args: argparse.Namespace) -> None:
    """Update task fields."""
    kanban = _get_kanban()
    fields: dict[str, Any] = {}
    if getattr(args, "title", None):
        fields["title"] = args.title
    if getattr(args, "desc", None) is not None:
        fields["description"] = args.desc
    if getattr(args, "priority", None):
        fields["priority"] = args.priority
    if getattr(args, "type", None):
        fields["type"] = args.type
    if getattr(args, "tags", None) is not None:
        fields["tags"] = args.tags
    if getattr(args, "board", None):
        fields["board"] = args.board
    if getattr(args, "due", None):
        try:
            fields["due_date"] = datetime.strptime(args.due, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            print(f"Invalid date format: {args.due}")
            kanban.close()
            return

    if not fields:
        print("No fields to update.")
        kanban.close()
        return

    ok = kanban.update_task(args.id, **fields)
    kanban.close()
    if ok:
        print(f"Task #{args.id} updated.")
    else:
        print(f"Task #{args.id} not found.")


def cmd_task_move(args: argparse.Namespace) -> None:
    """Change task status."""
    kanban = _get_kanban()
    ok = kanban.move(args.id, args.status)
    kanban.close()
    if ok:
        print(f"Task #{args.id} -> {args.status}")
    else:
        print(f"Failed to move task #{args.id}. Check ID and status value.")


def cmd_task_assign(args: argparse.Namespace) -> None:
    """Assign task to an agent."""
    kanban = _get_kanban()
    ok = kanban.assign(args.id, args.agent)
    kanban.close()
    if ok:
        print(f"Task #{args.id} assigned to {args.agent}")
    else:
        print(f"Task #{args.id} not found.")


def cmd_task_delete(args: argparse.Namespace) -> None:
    """Delete a task."""
    kanban = _get_kanban()
    ok = kanban.delete_task(args.id)
    kanban.close()
    if ok:
        print(f"Task #{args.id} deleted.")
    else:
        print(f"Task #{args.id} not found.")


def cmd_task_list(args: argparse.Namespace) -> None:
    """List tasks with optional filters."""
    kanban = _get_kanban()
    tasks = kanban.list_tasks(
        status=getattr(args, "status", "") or "",
        project=getattr(args, "project", "") or "",
        board=getattr(args, "board", "") or "",
        type=getattr(args, "type", "") or "",
        assigned_to=getattr(args, "assigned", "") or "",
    )
    kanban.close()

    if not tasks:
        print("No tasks found.")
        return

    for t in tasks:
        from openkeel.integrations.kanban import _priority_badge, _type_badge
        pri = _priority_badge(t.get("priority", "medium"))
        tb = _type_badge(t.get("type", "task"))
        assignee = f" @{t['assigned_to']}" if t.get("assigned_to") else ""
        board_tag = f" [{t['board']}]" if t.get("board", "default") != "default" else ""
        print(f"  #{t['id']:>3}  {t['status']:<12} {t['title']}{pri}{tb}{assignee}{board_tag}")


def cmd_task_search(args: argparse.Namespace) -> None:
    """Search tasks by keyword or semantic similarity."""
    kanban = _get_kanban()
    if getattr(args, "semantic", False):
        results = kanban.search_semantic(args.query, top_k=args.top, project=getattr(args, "project", "") or "")
    else:
        results = kanban.search_keyword(args.query, top_k=args.top, project=getattr(args, "project", "") or "")
    kanban.close()

    if not results:
        print("No matching tasks found.")
        return

    for t in results:
        score = t.get("score", 0)
        print(f"  #{t['id']:>3}  [{score:.2f}]  {t['status']:<12} {t['title']}")


def cmd_task_link(args: argparse.Namespace) -> None:
    """Link a task to a wiki page."""
    kanban = _get_kanban()
    ok = kanban.link_wiki(args.id, args.wiki_slug)
    kanban.close()
    if ok:
        print(f"Task #{args.id} linked to wiki:{args.wiki_slug}")
    else:
        print("Link failed. Check task ID and wiki slug exist.")


def cmd_task_from_journal(args: argparse.Namespace) -> None:
    """Promote a journal entry to a task."""
    kanban = _get_kanban()
    try:
        task_id = kanban.from_journal(
            args.journal_id,
            priority=getattr(args, "priority", "medium") or "medium",
            project=getattr(args, "project", "") or "",
        )
        print(f"Task #{task_id} created from journal entry #{args.journal_id}")
    except ValueError as exc:
        print(str(exc))
    kanban.close()


def cmd_task_stats(args: argparse.Namespace) -> None:
    """Show task statistics."""
    kanban = _get_kanban()
    s = kanban.stats(project=getattr(args, "project", "") or "")
    kanban.close()

    project_label = f" (project: {args.project})" if getattr(args, "project", "") else ""
    print(f"Task Statistics{project_label}")
    print(f"  Total: {s['total']}")

    if s["by_status"]:
        parts = [f"{k}: {v}" for k, v in sorted(s["by_status"].items())]
        print(f"  Status:   {', '.join(parts)}")
    if s["by_type"]:
        parts = [f"{k}: {v}" for k, v in sorted(s["by_type"].items())]
        print(f"  Type:     {', '.join(parts)}")
    if s["by_priority"]:
        parts = [f"{k}: {v}" for k, v in sorted(s["by_priority"].items())]
        print(f"  Priority: {', '.join(parts)}")
    if s["by_assignee"]:
        parts = [f"{k}: {v}" for k, v in sorted(s["by_assignee"].items())]
        print(f"  Assigned: {', '.join(parts)}")


def cmd_board(args: argparse.Namespace) -> None:
    """Show kanban board column view."""
    kanban = _get_kanban()
    project = getattr(args, "project", "") or ""
    board = getattr(args, "board", "") or ""
    view = kanban.board_view(project=project, board=board)
    kanban.close()

    total = sum(len(v) for v in view.values())
    if total == 0:
        print("No tasks found.")
        return

    # Detect terminal width
    try:
        term_width = shutil.get_terminal_size().columns
    except Exception:
        term_width = 80

    columns = [
        ("TODO", "todo"),
        ("IN PROGRESS", "in_progress"),
        ("DONE", "done"),
        ("BLOCKED", "blocked"),
    ]
    # Only show columns that have tasks (or TODO/IN PROGRESS always)
    active_cols = [
        (label, key) for label, key in columns
        if view[key] or key in ("todo", "in_progress")
    ]

    col_width = max(16, (term_width - 2) // len(active_cols) - 2)

    # Print headers
    header_line = "  ".join(label.ljust(col_width) for label, _ in active_cols)
    sep_line = "  ".join(("-" * len(label)).ljust(col_width) for label, _ in active_cols)
    print(f"\n{header_line}")
    print(sep_line)

    # Find max rows
    max_rows = max(len(view[key]) for _, key in active_cols) if active_cols else 0

    from openkeel.integrations.kanban import _priority_badge, _type_badge

    for row_idx in range(max_rows):
        cells = []
        for _, key in active_cols:
            tasks = view[key]
            if row_idx < len(tasks):
                t = tasks[row_idx]
                pri = _priority_badge(t.get("priority", "medium"))
                tb = _type_badge(t.get("type", "task"))
                cell = f"#{t['id']} {t['title']}{pri}{tb}"
                if len(cell) > col_width:
                    cell = cell[: col_width - 2] + ".."
                cells.append(cell.ljust(col_width))
            else:
                cells.append(" " * col_width)
        print("  ".join(cells))

    # Summary
    counts = {k: len(v) for k, v in view.items()}
    board_count = 1  # simplified
    print(
        f"\n  {total} tasks ({counts['todo']} todo, {counts['in_progress']} in progress, "
        f"{counts['done']} done, {counts['blocked']} blocked)"
    )


def cmd_board_list(args: argparse.Namespace) -> None:
    """List all boards."""
    kanban = _get_kanban()
    boards = kanban.list_boards(project=getattr(args, "project", "") or "")
    kanban.close()

    if not boards:
        print("No boards found.")
        return

    for b in boards:
        statuses = b.get("statuses", "")
        print(f"  {b['board']:<20} {b['count']} tasks  ({statuses})")


# ---------------------------------------------------------------------------
# Context refresh
# ---------------------------------------------------------------------------


def cmd_context_refresh(args: argparse.Namespace) -> None:
    """Rebuild session_context.json from journal + wiki + tasks."""
    journal = _get_journal()
    wiki = _get_wiki()
    kanban = _get_kanban()

    project = getattr(args, 'project', '') or ''

    journal_summary = journal.get_recent_narrative(project=project, limit=5)
    wiki_summary = wiki.get_relevant_pages(
        query=project if project else "project context",
        top_k=3,
    )
    task_summary = kanban.get_task_summary(project=project)

    # Build capsule line (one-liner for PreToolUse)
    capsule_parts = []
    if journal_summary:
        # Extract first title from journal
        for line in journal_summary.split('\n'):
            if line.startswith('### '):
                capsule_parts.append(line[4:].strip()[:60])
                break
    if wiki_summary:
        for line in wiki_summary.split('\n'):
            if line.startswith('### '):
                capsule_parts.append(line[4:].strip()[:60])
                break
    if task_summary:
        # Count active (non-done) tasks for capsule
        active = task_summary.count("- #")
        if active:
            capsule_parts.append(f"{active} active tasks")

    capsule_line = " | ".join(capsule_parts) if capsule_parts else ""

    context = {
        "journal_summary": journal_summary,
        "wiki_summary": wiki_summary,
        "task_summary": task_summary,
        "capsule_line": capsule_line,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    context_path = Path.home() / ".openkeel" / "session_context.json"
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(json.dumps(context, indent=2), encoding="utf-8")

    print(f"Context refreshed: {context_path}")
    if journal_summary:
        lines = journal_summary.count('\n')
        print(f"  Journal: {lines} lines")
    else:
        print("  Journal: (empty)")
    if wiki_summary:
        lines = wiki_summary.count('\n')
        print(f"  Wiki: {lines} lines")
    else:
        print("  Wiki: (empty)")
    if task_summary:
        lines = task_summary.count('\n')
        print(f"  Tasks: {lines} lines")
    else:
        print("  Tasks: (empty)")
    if capsule_line:
        print(f"  Capsule: {capsule_line[:80]}")

    journal.close()
    wiki.close()
    kanban.close()


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
        version="%(prog)s 0.2.0",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # launch (also the default when no subcommand given)
    p_launch = sub.add_parser("launch", help="Launch an agent with context injection (default).")
    p_launch.add_argument("agent", nargs="?", default="", help="Agent name (claude, gemini, codex).")
    p_launch.set_defaults(func=cmd_launch)

    # init
    p_init = sub.add_parser("init", help="Interactive setup wizard.")
    p_init.set_defaults(func=cmd_init)

    # install
    p_install = sub.add_parser("install", help="Generate hooks, wire into agent settings.")
    p_install.add_argument(
        "--profile", "-p",
        help="Profile name to load FV hooks config from (e.g. pentesting).",
    )
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

    # profile
    p_profile = sub.add_parser("profile", help="Profile management (full mode).")
    profile_sub = p_profile.add_subparsers(dest="profile_command", metavar="<subcommand>")
    profile_sub.required = True

    p_prof_list = profile_sub.add_parser("list", help="List available profiles.")
    p_prof_list.set_defaults(func=cmd_profile_list)

    p_prof_show = profile_sub.add_parser("show", help="Display profile details.")
    p_prof_show.add_argument("name", help="Profile name or path.")
    p_prof_show.set_defaults(func=cmd_profile_show)

    p_prof_validate = profile_sub.add_parser("validate", help="Validate a profile YAML.")
    p_prof_validate.add_argument("file", help="Profile name or path to validate.")
    p_prof_validate.set_defaults(func=cmd_profile_validate)

    # run
    p_run = sub.add_parser(
        "run",
        help="Launch an agent with proxy shell (full mode).",
        description="Run an agent subprocess with SHELL=openkeel-exec for command interception.",
    )
    p_run.add_argument("--profile", "-p", required=True, help="Profile name or path.")
    p_run.add_argument("agent_command", nargs=argparse.REMAINDER, help="Agent command and arguments (after --).")
    p_run.set_defaults(func=cmd_run)

    # history
    p_history = sub.add_parser("history", help="Query session history.")
    p_history.add_argument("--project", help="Filter by project name.")
    p_history.add_argument("--status", help="Filter by status (running/completed/failed/interrupted).")
    p_history.add_argument("--search", "-s", help="Full-text search across events.")
    p_history.add_argument("--stats", action="store_true", help="Show aggregate statistics.")
    p_history.add_argument("--session", help="Show details for a specific session ID.")
    p_history.set_defaults(func=cmd_history)

    # phase
    p_phase = sub.add_parser("phase", help="Phase management (full mode).")
    phase_sub = p_phase.add_subparsers(dest="phase_command", metavar="<subcommand>")
    phase_sub.required = True

    p_phase_show = phase_sub.add_parser("show", help="Show current phase status.")
    p_phase_show.add_argument("--session", required=True, help="Session ID.")
    p_phase_show.set_defaults(func=cmd_phase_show)

    p_phase_next = phase_sub.add_parser("next", help="Advance to the next phase.")
    p_phase_next.add_argument("--session", required=True, help="Session ID.")
    p_phase_next.add_argument("--force", action="store_true", help="Skip gate checks.")
    p_phase_next.set_defaults(func=cmd_phase_next)

    # -- remember / recall / memory ------------------------------------------

    p_remember = sub.add_parser("remember", help="Store a fact in local memory.")
    p_remember.add_argument("fact", help="The fact to remember.")
    p_remember.add_argument("--project", "-p", default="", help="Project name.")
    p_remember.add_argument("--tag", "-t", default="", help="Tag (e.g. decision, bug, note).")
    p_remember.add_argument("--source", default="", help="Source of the fact.")
    p_remember.set_defaults(func=cmd_remember)

    p_recall = sub.add_parser("recall", help="Search local memory.")
    p_recall.add_argument("query", help="Search query.")
    p_recall.add_argument("--top", "-n", type=int, default=5, help="Number of results.")
    p_recall.add_argument("--project", "-p", default="", help="Filter by project.")
    p_recall.set_defaults(func=cmd_recall)

    p_memory = sub.add_parser("memory", help="Memory management.")
    memory_sub = p_memory.add_subparsers(dest="memory_command", metavar="<subcommand>")

    p_mem_stats = memory_sub.add_parser("stats", help="Show memory statistics.")
    p_mem_stats.set_defaults(func=cmd_memory_stats)

    p_mem_recent = memory_sub.add_parser("recent", help="Show recent facts.")
    p_mem_recent.add_argument("--limit", "-n", type=int, default=10)
    p_mem_recent.add_argument("--project", "-p", default="")
    p_mem_recent.set_defaults(func=cmd_memory_recent)

    p_mem_export = memory_sub.add_parser("export", help="Export all facts as JSONL.")
    p_mem_export.set_defaults(func=cmd_memory_export)

    p_mem_delete = memory_sub.add_parser("delete", help="Delete a fact by ID.")
    p_mem_delete.add_argument("id", type=int, help="Fact ID to delete.")
    p_mem_delete.set_defaults(func=cmd_memory_delete)

    p_memory.set_defaults(func=lambda a: cmd_memory_stats(a) if not getattr(a, 'memory_command', None) else None)

    # timer
    p_timer = sub.add_parser("timer", help="Self-timer management.")
    timer_sub = p_timer.add_subparsers(dest="timer_command", metavar="<subcommand>")
    timer_sub.required = True

    p_timer_add = timer_sub.add_parser("add", help="Set a self-timer.")
    p_timer_add.add_argument("message", help="Reminder message.")
    p_timer_add.add_argument("--in", dest="in_duration", required=True, help="Duration until fire (e.g. 5m, 1h, 2h30m).")
    p_timer_add.add_argument("--repeat", action="store_true", help="Repeat at the same interval.")
    p_timer_add.set_defaults(func=cmd_timer_add)

    p_timer_list = timer_sub.add_parser("list", help="Show active timers.")
    p_timer_list.set_defaults(func=cmd_timer_list)

    p_timer_remove = timer_sub.add_parser("remove", help="Cancel a timer by ID.")
    p_timer_remove.add_argument("timer_id", help="Timer ID to remove.")
    p_timer_remove.set_defaults(func=cmd_timer_remove)

    p_timer_clear = timer_sub.add_parser("clear", help="Remove all timers.")
    p_timer_clear.set_defaults(func=cmd_timer_clear)

    # -- journal ---------------------------------------------------------------

    p_journal = sub.add_parser("journal", help="Session journal management.")
    journal_sub = p_journal.add_subparsers(dest="journal_command", metavar="<subcommand>")
    journal_sub.required = True

    p_jadd = journal_sub.add_parser("add", help="Add a journal entry.")
    p_jadd.add_argument("body", help="Entry body text.")
    p_jadd.add_argument("--title", "-T", default="", help="Entry title.")
    p_jadd.add_argument("--project", "-p", default="", help="Project name.")
    p_jadd.add_argument("--entry-type", default="manual", help="Entry type (manual/session_end/milestone).")
    p_jadd.add_argument("--tags", "-t", default="", help="Comma-separated tags.")
    p_jadd.add_argument("--session-id", default="", help="Session identifier.")
    p_jadd.add_argument("--mission-name", default="", help="Associated mission name.")
    p_jadd.set_defaults(func=cmd_journal_add)

    p_jshow = journal_sub.add_parser("show", help="Show recent journal entries.")
    p_jshow.add_argument("--project", "-p", default="", help="Filter by project.")
    p_jshow.add_argument("--limit", "-n", type=int, default=10, help="Number of entries.")
    p_jshow.add_argument("--entry-type", default="", help="Filter by entry type.")
    p_jshow.set_defaults(func=cmd_journal_show)

    p_jsearch = journal_sub.add_parser("search", help="Search journal entries.")
    p_jsearch.add_argument("query", help="Search query.")
    p_jsearch.add_argument("--top", "-n", type=int, default=10, help="Number of results.")
    p_jsearch.add_argument("--project", "-p", default="", help="Filter by project.")
    p_jsearch.add_argument("--semantic", "-s", action="store_true", help="Use semantic search (requires embeddings server).")
    p_jsearch.set_defaults(func=cmd_journal_search)

    p_jflush = journal_sub.add_parser("flush", help="Flush enforcement log to journal.")
    p_jflush.add_argument("--project", "-p", default="", help="Project name.")
    p_jflush.add_argument("--session-id", default="", help="Session identifier.")
    p_jflush.set_defaults(func=cmd_journal_flush)

    # -- wiki ------------------------------------------------------------------

    p_wiki = sub.add_parser("wiki", help="Knowledge wiki management.")
    wiki_sub = p_wiki.add_subparsers(dest="wiki_command", metavar="<subcommand>")
    wiki_sub.required = True

    p_wadd = wiki_sub.add_parser("add", help="Add or append to a wiki page.")
    p_wadd.add_argument("title", help="Page title.")
    p_wadd.add_argument("body", help="Page body content.")
    p_wadd.add_argument("--category", "-c", default="", help="Page category.")
    p_wadd.add_argument("--project", "-p", default="", help="Project name.")
    p_wadd.add_argument("--tags", "-t", default="", help="Comma-separated tags.")
    p_wadd.set_defaults(func=cmd_wiki_add)

    p_wshow = wiki_sub.add_parser("show", help="Display a wiki page.")
    p_wshow.add_argument("slug", help="Page slug.")
    p_wshow.set_defaults(func=cmd_wiki_show)

    p_wlist = wiki_sub.add_parser("list", help="List wiki pages.")
    p_wlist.add_argument("--category", "-c", default="", help="Filter by category.")
    p_wlist.add_argument("--project", "-p", default="", help="Filter by project.")
    p_wlist.set_defaults(func=cmd_wiki_list)

    p_wcats = wiki_sub.add_parser("categories", help="List wiki categories.")
    p_wcats.set_defaults(func=cmd_wiki_categories)

    p_wsearch = wiki_sub.add_parser("search", help="Search wiki pages.")
    p_wsearch.add_argument("query", help="Search query.")
    p_wsearch.add_argument("--top", "-n", type=int, default=10, help="Number of results.")
    p_wsearch.add_argument("--semantic", "-s", action="store_true", help="Use semantic search (requires embeddings server).")
    p_wsearch.set_defaults(func=cmd_wiki_search)

    p_wlink = wiki_sub.add_parser("link", help="Create a cross-reference between pages.")
    p_wlink.add_argument("from_slug", help="Source page slug.")
    p_wlink.add_argument("to_slug", help="Target page slug.")
    p_wlink.set_defaults(func=cmd_wiki_link)

    p_wfromj = wiki_sub.add_parser("from-journal", help="Promote a journal entry to a wiki page.")
    p_wfromj.add_argument("journal_id", type=int, help="Journal entry ID.")
    p_wfromj.add_argument("--title", "-T", default="", help="Override title.")
    p_wfromj.add_argument("--category", "-c", default="", help="Page category.")
    p_wfromj.set_defaults(func=cmd_wiki_from_journal)

    # -- task ------------------------------------------------------------------

    p_task = sub.add_parser("task", help="Task management (kanban / todo tracker).")
    task_sub = p_task.add_subparsers(dest="task_command", metavar="<subcommand>")
    task_sub.required = True

    p_tadd = task_sub.add_parser("add", help="Create a new task.")
    p_tadd.add_argument("title", help="Task title.")
    p_tadd.add_argument("-d", "--desc", default="", help="Task description.")
    p_tadd.add_argument("-p", "--project", default="", help="Project name.")
    p_tadd.add_argument("--priority", default="medium", choices=["low", "medium", "high", "critical"], help="Priority level.")
    p_tadd.add_argument("--type", default="task", choices=["task", "bug", "feature", "idea"], help="Task type.")
    p_tadd.add_argument("--board", default="default", help="Board name (e.g. backlog, sprint-1).")
    p_tadd.add_argument("--assign", default="", help="Assign to agent.")
    p_tadd.add_argument("-t", "--tags", default="", help="Comma-separated tags.")
    p_tadd.add_argument("--due", default="", help="Due date (YYYY-MM-DD).")
    p_tadd.add_argument("--parent", type=int, default=None, help="Parent task ID (subtask).")
    p_tadd.set_defaults(func=cmd_task_add)

    p_tshow = task_sub.add_parser("show", help="Show task details.")
    p_tshow.add_argument("id", type=int, help="Task ID.")
    p_tshow.set_defaults(func=cmd_task_show)

    p_tedit = task_sub.add_parser("edit", help="Update task fields.")
    p_tedit.add_argument("id", type=int, help="Task ID.")
    p_tedit.add_argument("--title", default=None, help="New title.")
    p_tedit.add_argument("--desc", default=None, help="New description.")
    p_tedit.add_argument("--priority", default=None, choices=["low", "medium", "high", "critical"], help="New priority.")
    p_tedit.add_argument("--type", default=None, choices=["task", "bug", "feature", "idea"], help="New type.")
    p_tedit.add_argument("-t", "--tags", default=None, help="New tags.")
    p_tedit.add_argument("--board", default=None, help="New board.")
    p_tedit.add_argument("--due", default=None, help="New due date (YYYY-MM-DD).")
    p_tedit.set_defaults(func=cmd_task_edit)

    p_tmove = task_sub.add_parser("move", help="Change task status.")
    p_tmove.add_argument("id", type=int, help="Task ID.")
    p_tmove.add_argument("status", choices=["todo", "in_progress", "done", "blocked"], help="New status.")
    p_tmove.set_defaults(func=cmd_task_move)

    p_tassign = task_sub.add_parser("assign", help="Assign task to an agent.")
    p_tassign.add_argument("id", type=int, help="Task ID.")
    p_tassign.add_argument("agent", help="Agent name.")
    p_tassign.set_defaults(func=cmd_task_assign)

    p_tdelete = task_sub.add_parser("delete", help="Delete a task.")
    p_tdelete.add_argument("id", type=int, help="Task ID.")
    p_tdelete.set_defaults(func=cmd_task_delete)

    p_tlist = task_sub.add_parser("list", help="List/filter tasks.")
    p_tlist.add_argument("--status", default="", help="Filter by status.")
    p_tlist.add_argument("-p", "--project", default="", help="Filter by project.")
    p_tlist.add_argument("--board", default="", help="Filter by board.")
    p_tlist.add_argument("--type", default="", help="Filter by type.")
    p_tlist.add_argument("--assigned", default="", help="Filter by assignee.")
    p_tlist.set_defaults(func=cmd_task_list)

    p_tsearch = task_sub.add_parser("search", help="Search tasks.")
    p_tsearch.add_argument("query", help="Search query.")
    p_tsearch.add_argument("--top", "-n", type=int, default=10, help="Number of results.")
    p_tsearch.add_argument("-p", "--project", default="", help="Filter by project.")
    p_tsearch.add_argument("--semantic", "-s", action="store_true", help="Use semantic search.")
    p_tsearch.set_defaults(func=cmd_task_search)

    p_tlink = task_sub.add_parser("link", help="Link task to a wiki page.")
    p_tlink.add_argument("id", type=int, help="Task ID.")
    p_tlink.add_argument("wiki_slug", help="Wiki page slug.")
    p_tlink.set_defaults(func=cmd_task_link)

    p_tfromj = task_sub.add_parser("from-journal", help="Promote journal entry to task.")
    p_tfromj.add_argument("journal_id", type=int, help="Journal entry ID.")
    p_tfromj.add_argument("--priority", default="medium", help="Task priority.")
    p_tfromj.add_argument("-p", "--project", default="", help="Project name.")
    p_tfromj.set_defaults(func=cmd_task_from_journal)

    p_tstats = task_sub.add_parser("stats", help="Show task statistics.")
    p_tstats.add_argument("-p", "--project", default="", help="Filter by project.")
    p_tstats.set_defaults(func=cmd_task_stats)

    # -- board -----------------------------------------------------------------

    p_board = sub.add_parser("board", help="Kanban board view.")
    p_board.add_argument("project", nargs="?", default="", help="Project name.")
    p_board.add_argument("--board", default="", help="Board name filter.")
    p_board.set_defaults(func=cmd_board)

    p_board_list = sub.add_parser("board-list", help="List all boards.")
    p_board_list.add_argument("-p", "--project", default="", help="Filter by project.")
    p_board_list.set_defaults(func=cmd_board_list)

    # -- serve (embeddings server) ---------------------------------------------

    p_serve = sub.add_parser("serve", help="Start the embeddings server.")
    p_serve.add_argument("--port", type=int, default=7437, help="Server port (default: 7437).")
    p_serve.set_defaults(func=cmd_serve)

    p_serve_status = sub.add_parser("serve-status", help="Check embeddings server status.")
    p_serve_status.set_defaults(func=cmd_serve_status)

    p_reindex = sub.add_parser("reindex", help="Rebuild all embeddings from journal + wiki.")
    p_reindex.set_defaults(func=cmd_reindex)

    # -- context ---------------------------------------------------------------

    p_context = sub.add_parser("context", help="Context management.")
    context_sub = p_context.add_subparsers(dest="context_command", metavar="<subcommand>")
    context_sub.required = True

    p_ctx_refresh = context_sub.add_parser("refresh", help="Rebuild session_context.json.")
    p_ctx_refresh.add_argument("--project", "-p", default="", help="Project name for context.")
    p_ctx_refresh.set_defaults(func=cmd_context_refresh)

    args = parser.parse_args()

    # Default to launch if no subcommand given
    if not args.command:
        from openkeel.launch import launch
        launch()
        return

    args.func(args)


if __name__ == "__main__":
    main()
