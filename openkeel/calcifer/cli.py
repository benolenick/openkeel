#!/usr/bin/env python3
"""Calcifer v2 CLI: Command-line interface for the broker agent.

Usage:
  calcifer "what's the status of the watcher?"
  calcifer --session mysession "follow up question"
  calcifer --new "start fresh"
  calcifer --json "get structured output"
"""

import sys
import json
import argparse
from pathlib import Path
from openkeel.calcifer.broker_session import BrokerSession
from openkeel.calcifer.routing_policy import RoutingPolicy


class CalciferCLI:
    """CLI interface for Calcifer broker."""

    SESSIONS_DIR = Path.home() / ".calcifer" / "sessions"

    def __init__(self):
        self.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    def run(self):
        """Parse args and execute."""
        parser = argparse.ArgumentParser(
            description="Calcifer v2: Intelligent CLI agent with band routing",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Examples:
  calcifer "what time is it"
  calcifer --session mywork "now explain that"
  calcifer --new "fresh session"
  calcifer --json "get JSON output"
            """,
        )

        parser.add_argument("prompt", nargs="?", help="Message to send (or read from stdin if omitted)")
        parser.add_argument("--session", type=str, default=None, help="Session name (default: anonymous)")
        parser.add_argument("--new", action="store_true", help="Start fresh session (ignore history)")
        parser.add_argument("--json", action="store_true", help="Output as JSON (include metadata)")
        parser.add_argument("--verbose", action="store_true", help="Show routing decisions")
        parser.add_argument("--context", action="store_true", help="Show message history")
        parser.add_argument("--preset", type=str, choices=["cheap", "balanced", "quality", "local"], help="Use routing preset")
        parser.add_argument("--settings", choices=["show", "reset"], help="Show or reset routing settings")
        parser.add_argument("--presets", action="store_true", help="List available presets")
        # Per-band model overrides
        parser.add_argument("--band-a-model", choices=["haiku", "sonnet", "opus"], metavar="MODEL", help="Model for Band A (chat/trivial)")
        parser.add_argument("--band-b-model", choices=["direct", "sonnet", "opus"], metavar="MODEL", help="Model for Band B (simple reads)")
        parser.add_argument("--band-c-model", choices=["sonnet", "opus"], metavar="MODEL", help="Model for Band C (standard task)")
        parser.add_argument("--band-d-model", choices=["opus", "sonnet"], metavar="MODEL", help="Model for Band D (hard/multi-step)")
        parser.add_argument("--band-e-model", choices=["opus", "sonnet"], metavar="MODEL", help="Model for Band E (escalated)")
        parser.add_argument("--judge-model", choices=["opus", "sonnet"], metavar="MODEL", help="Model for judgment step")

        args = parser.parse_args()

        # Handle routing settings commands
        if args.presets:
            from openkeel.calcifer.routing_policy import PRESETS, BAND_VALID_MODELS
            print("Available presets:")
            for name, models in PRESETS.items():
                print(f"  {name:<12}", " | ".join(f"{k}={v}" for k, v in models.items()))
            sys.exit(0)
        if args.settings == "show":
            policy = RoutingPolicy.load(preset=args.preset)
            print(policy.show())
            sys.exit(0)
        if args.settings == "reset":
            from openkeel.calcifer.routing_policy import CONFIG_PATH
            CONFIG_PATH.unlink(missing_ok=True)
            print("Settings reset to defaults.")
            sys.exit(0)

        # Build routing policy from preset + per-band overrides
        policy = RoutingPolicy.load(preset=args.preset)
        overrides = {}
        for slot, attr in [("band_a", "band_a_model"), ("band_b", "band_b_model"),
                           ("band_c", "band_c_model"), ("band_d", "band_d_model"),
                           ("band_e", "band_e_model"), ("judge", "judge_model")]:
            val = getattr(args, attr, None)
            if val:
                overrides[slot] = val
        if overrides:
            policy = policy.with_override(**overrides)

        # Get prompt
        if args.prompt:
            prompt = args.prompt
        elif not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        else:
            parser.print_help()
            sys.exit(1)

        # Determine session
        if args.new:
            session_id = None  # Fresh session
        else:
            session_id = args.session or "default"

        # Create session
        session = BrokerSession(session_id=session_id, verbose=args.verbose, policy=policy)

        # Load history if persistent session
        if args.session and not args.new:
            self._load_session_history(session, args.session)

        # Send message
        response, metadata = session.send_message(prompt)

        # Output
        if args.json:
            output = {
                "response": response,
                "metadata": metadata,
            }
            if args.context:
                output["history"] = [
                    {"user": u, "assistant": a} for u, a in session._message_history
                ]
            print(json.dumps(output, indent=2))
        else:
            print(response)
            if args.context:
                print(f"\n[Session: {session.session_id} | Band: {metadata['band']} | {metadata['latency']:.1f}s]")

        # Save history if persistent session
        if args.session and not args.new:
            self._save_session_history(session, args.session)

        # Exit with status
        sys.exit(0 if metadata["success"] else 1)

    def _load_session_history(self, session: BrokerSession, session_name: str):
        """Load previous conversation history."""
        history_file = self.SESSIONS_DIR / f"{session_name}.json"
        if history_file.exists():
            try:
                data = json.loads(history_file.read_text())
                session._message_history = [tuple(pair) for pair in data.get("history", [])]
                if session.verbose:
                    print(f"[Loaded {len(session._message_history)} prior messages]")
            except Exception as e:
                print(f"Warning: Could not load session history: {e}", file=sys.stderr)

    def _save_session_history(self, session: BrokerSession, session_name: str):
        """Save conversation history for next turn."""
        history_file = self.SESSIONS_DIR / f"{session_name}.json"
        try:
            data = {
                "session_id": session.session_id,
                "history": list(session._message_history),
            }
            history_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"Warning: Could not save session history: {e}", file=sys.stderr)


def main():
    """Entry point."""
    cli = CalciferCLI()
    cli.run()


if __name__ == "__main__":
    main()
