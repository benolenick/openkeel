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

        args = parser.parse_args()

        # Handle routing settings commands
        if args.presets:
            RoutingPolicy.show_presets()
            sys.exit(0)
        if args.settings == "show":
            RoutingPolicy.show_config()
            sys.exit(0)
        if args.settings == "reset":
            RoutingPolicy.CONFIG_FILE.unlink(missing_ok=True)
            print("Settings reset to defaults")
            sys.exit(0)

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
        session = BrokerSession(session_id=session_id, verbose=args.verbose)

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
