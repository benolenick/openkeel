"""CLI entry point for OpenKeel 2.0."""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="openkeel",
        description="OpenKeel 2.0 — AI agent toolkit with token-saving delegation + long-term memory",
    )
    parser.add_argument("task", nargs="?", help="Run a bubble analysis task (or 'chat' for interactive REPL)")
    parser.add_argument("--repo", default=None, help="Repository path (default: cwd)")
    parser.add_argument("--headless", action="store_true", help="Run without GUI")
    parser.add_argument("--status", action="store_true", help="Show current status")
    parser.add_argument("--version", action="version", version="openkeel 2.0.0")
    args = parser.parse_args()

    if args.status:
        _show_status()
        return

    if args.task == "chat":
        _run_chat(args)
        return

    if args.task or args.headless:
        _run_headless(args)
        return

    # Default: launch GUI
    try:
        from openkeel.gui.app import main as gui_main
        gui_main()
    except ImportError as e:
        print(f"GUI unavailable ({e}). Use --headless for CLI mode.", file=sys.stderr)
        sys.exit(1)


def _show_status():
    from openkeel.gui.settings import load_settings
    from openkeel.quota import get_usage

    s = load_settings()
    u = get_usage()

    print("OpenKeel 2.0")
    print(f"  CLI model:  {s.get('cli_model', 'sonnet')}")
    print(f"  Runner:     {s.get('runner', 'haiku_api')}")
    print(f"  Routing:    {s.get('routing', 'flat')}")
    print(f"  Local model:{s.get('local_model', 'none')}")
    print()

    try:
        from openkeel.hyphae import is_available
        print(f"  Hyphae:     {'connected' if is_available() else 'offline'}")
    except Exception:
        print("  Hyphae:     error")

    if s.get("runner") == "local":
        try:
            import urllib.request, json
            with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as r:
                data = json.loads(r.read())
                models = [m["name"] for m in data.get("models", [])]
                print(f"  Ollama:     {', '.join(models) or 'no models loaded'}")
        except Exception:
            print("  Ollama:     offline")

    print()
    print(f"  Quota:      {u['pct']:.1f}% ({u['runs']} runs this week)")
    print(f"  BPH:        {u['bph']:.1f}%/hr")
    print(f"  Sonnet:     {u['sonnet_calls']} calls")


def _run_headless(args):
    import os
    from openkeel.gui.settings import load_settings

    s = load_settings()
    repo = args.repo or os.getcwd()
    task = args.task

    if not task:
        print("No task provided. Usage: openkeel 'your task' --repo /path", file=sys.stderr)
        sys.exit(1)

    from openkeel.bubble.engine import run
    output, cost, log = run(task, repo, verbose=True, local_mode=s.get("runner", "haiku_api"))
    print(output)


def _run_chat(args):
    """Interactive bubble REPL — each turn = Haiku gather + Sonnet CLI synthesize."""
    import os
    from openkeel.gui.settings import load_settings
    from openkeel.bubble.engine import run

    s = load_settings()
    repo = args.repo or os.getcwd()
    runner = s.get("runner", "haiku_api")

    # Banner
    print("\033[1;36m" + "=" * 60 + "\033[0m")
    print("\033[1;36mOpenKeel Bubble Chat\033[0m  —  Haiku gathers, Sonnet synthesizes")
    print(f"\033[2mrepo: {repo}   runner: {runner}   (Ctrl-D or 'exit' to quit)\033[0m")
    print("\033[1;36m" + "=" * 60 + "\033[0m")
    print()

    # Conversation history — keep last N turns (used for chat continuity in the
    # bubble *prompt*; cache reuse across Sonnet calls is handled separately
    # via session_id below).
    history = []
    MAX_HISTORY_TURNS = 3

    # Sonnet CLI session id — reused across turns so Anthropic's prompt cache
    # hits and we don't re-pay cache_creation on every turn.
    session_id = None

    while True:
        try:
            user_input = input("\033[1;33m> \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", ":q"):
            print("bye")
            return

        # Build task with conversation context
        if history:
            ctx_parts = ["## Recent conversation\n"]
            for u, a in history[-MAX_HISTORY_TURNS:]:
                ctx_parts.append(f"User: {u}\nAssistant: {a[:1500]}\n")
            ctx_parts.append(f"\n## Current request\n{user_input}")
            task = "\n".join(ctx_parts)
        else:
            task = user_input

        try:
            output, cost, log = run(
                task, repo, verbose=False, local_mode=runner, session_id=session_id
            )
            # Capture the persisted Sonnet session id so the next turn resumes it.
            if isinstance(log, dict) and log.get("session_id"):
                session_id = log["session_id"]
        except Exception as e:
            print(f"\033[31m[bubble error] {e}\033[0m\n")
            continue

        # Print response
        print()
        print(output)
        print()
        # Cost line
        gathered = log.get("gather", {}).get("gathered_len", 0) if isinstance(log, dict) else 0
        wall_ms = log.get("wall_ms", 0) if isinstance(log, dict) else 0
        print(f"\033[2m[bubble] ${cost:.4f}  {wall_ms}ms  {gathered} chars gathered\033[0m")
        print()

        history.append((user_input, output or ""))


if __name__ == "__main__":
    main()
