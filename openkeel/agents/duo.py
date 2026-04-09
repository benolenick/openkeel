"""Duo launcher — starts Director + Operator + Critic as a coordinated team.

Usage:
    openkeel duo "Fix the login bug in auth.py"
    openkeel duo "Improve SC2 Commander" -d /home/om/sc2-commander --test "python3 -m pytest"
    openkeel duo --director-only "Refactor the settings module"
    openkeel duo --operator-only

The agents run as separate threads communicating through the Command Board API.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time

from openkeel.agents.director import Director
from openkeel.agents.operator import Operator
from openkeel.agents.critic import Critic


def _banner(goal: str = "", agents: list[str] | None = None):
    print("\033[1;35m" + "=" * 60)
    print("  OpenKeel Duo — Multi-Agent System")
    print("=" * 60 + "\033[0m")
    if goal:
        print(f"\033[1mGoal:\033[0m {goal}")
    if agents:
        print(f"\033[1mAgents:\033[0m {', '.join(agents)}")
    print()


def run_duo(goal: str, working_dir: str = "", model: str = "sonnet",
            director_only: bool = False, operator_only: bool = False,
            poll_interval: int = 30, operator_model: str = "",
            no_critic: bool = False, test_commands: list[str] | None = None,
            test_command: str = "", max_cycles: int = 0):
    """Launch the Director, Operator, and Critic."""

    working_dir = working_dir or os.getcwd()
    operator_model = operator_model or model

    director = None
    operator = None
    critic = None
    threads = []

    def _sigint(sig, frame):
        print("\n\033[1;31m[Duo] Ctrl+C — shutting down...\033[0m")
        if director:
            director.stop()
        if operator:
            operator.stop()
        if critic:
            critic.stop()

    signal.signal(signal.SIGINT, _sigint)

    agent_names = []

    if not operator_only:
        director = Director(
            goal=goal,
            poll_interval=poll_interval,
            model=model,
            working_dir=working_dir,
            use_critic=not no_critic,
            test_commands=test_commands,
            test_command=test_command,
            max_cycles=max_cycles,
        )
        t = threading.Thread(target=director.run, name="director", daemon=True)
        threads.append(("Director", t))
        agent_names.append("Director")

        # Critic runs alongside Director (it's called by Director, but also
        # registers with the board for visibility)
        if not no_critic:
            critic = Critic(
                working_dir=working_dir,
                test_commands=test_commands or [],
            )
            # Register critic so it shows up on the dashboard
            from openkeel.agents.critic import register_agent as reg_critic
            reg_critic()
            agent_names.append("Critic")

    if not director_only:
        operator = Operator(
            working_dir=working_dir,
            model=operator_model,
        )
        t = threading.Thread(target=operator.run, name="operator", daemon=True)
        threads.append(("Operator", t))
        agent_names.append("Operator")

    if operator_only:
        _banner("(Operator mode — waiting for directives)", agent_names)
    else:
        _banner(goal, agent_names)

    # Start all threads
    for name, t in threads:
        print(f"\033[1;32m[Duo]\033[0m Starting {name}...")
        t.start()

    # Wait for threads to finish
    try:
        while any(t.is_alive() for _, t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\033[1;31m[Duo] Interrupted.\033[0m")
        if director:
            director.stop()
        if operator:
            operator.stop()
        if critic:
            critic.stop()
        for _, t in threads:
            t.join(timeout=5)

    print("\033[1;35m[Duo] Session ended.\033[0m")


def main():
    """CLI entry point for `openkeel duo`."""
    parser = argparse.ArgumentParser(
        prog="openkeel duo",
        description="Launch Director/Operator/Critic multi-agent system.",
    )
    parser.add_argument("goal", nargs="?", default="",
                        help="The goal for the Director to plan and execute.")
    parser.add_argument("--working-dir", "-d", default="",
                        help="Working directory for the Operator (default: cwd).")
    parser.add_argument("--model", "-m", default="sonnet",
                        help="Model for Director reasoning (default: sonnet).")
    parser.add_argument("--operator-model", default="",
                        help="Model for Operator execution (default: same as --model).")
    parser.add_argument("--director-only", action="store_true",
                        help="Only run the Director (Operator launched separately).")
    parser.add_argument("--operator-only", action="store_true",
                        help="Only run the Operator (waits for directives).")
    parser.add_argument("--no-critic", action="store_true",
                        help="Disable the Critic agent (no quality reviews).")
    parser.add_argument("--test", action="append", dest="test_commands", default=[],
                        help="Test command for Critic to run per-step (repeatable).")
    parser.add_argument("--test-command", default="",
                        help="End-of-cycle test command (e.g. 'bash run_visual.sh').")
    parser.add_argument("--max-cycles", type=int, default=0,
                        help="Max improvement cycles (0 = infinite).")
    parser.add_argument("--poll", type=int, default=30,
                        help="Director poll interval in seconds (default: 30).")

    args = parser.parse_args()

    if not args.operator_only and not args.goal:
        parser.error("A goal is required unless using --operator-only")

    run_duo(
        goal=args.goal,
        working_dir=args.working_dir,
        model=args.model,
        director_only=args.director_only,
        operator_only=args.operator_only,
        poll_interval=args.poll,
        operator_model=args.operator_model,
        no_critic=args.no_critic,
        test_commands=args.test_commands,
        test_command=args.test_command,
        max_cycles=args.max_cycles,
    )


if __name__ == "__main__":
    main()
