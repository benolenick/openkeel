"""OpenKeel CLI entry point.

Subcommands
-----------
init         Interactive first-run setup.
install      Generate hooks, wire into agent settings.
status       Show constitution + active mission.
constitution show|test  Display or test rules.
mission      start|show|update|plan|finding|end  Mission management.
profile      list|show|validate  Profile management (full mode).
run          Launch an agent subprocess with proxy shell (full mode).
history      Query session history.
phase        next|show  Phase management (full mode).
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import uuid
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
    project = args.project or ""
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
    p_run.add_argument("--project", help="Project name (for history tracking).")
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
