"""CLI entry point for OpenKeel 2.0."""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="openkeel",
        description="OpenKeel 2.0 — AI agent toolkit with token-saving delegation + long-term memory",
    )
    parser.add_argument("task", nargs="?", help="Run a bubble analysis task (headless mode)")
    parser.add_argument("--repo", default=None, help="Repository path (default: cwd)")
    parser.add_argument("--headless", action="store_true", help="Run without GUI")
    parser.add_argument("--status", action="store_true", help="Show current status")
    parser.add_argument("--version", action="version", version="openkeel 2.0.0")
    args = parser.parse_args()

    if args.status:
        _show_status()
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

    # Hyphae
    try:
        from openkeel.hyphae import is_available
        print(f"  Hyphae:     {'connected' if is_available() else 'offline'}")
    except Exception:
        print("  Hyphae:     error")

    # Ollama
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

    # Use bubble engine
    from openkeel.bubble.engine import run
    output, cost, log = run(task, repo, verbose=True, local_mode=s.get("runner", "haiku_api"))
    print(output)


if __name__ == "__main__":
    main()
