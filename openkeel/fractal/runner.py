"""Fractal Runner — drives the decomposition loop with a real agent.

This is the piece that makes the fractal engine actually execute work.
It connects a FractalMethod to a real LLM backend (Claude CLI, Ollama,
or any callable), drives the loop, reports to the kanban board, and
handles resume on crash/reboot.

Usage:
    # Start a new fractal job
    runner = FractalRunner.start(
        title="Build scraper for RealEstate.com",
        description="Scrape all listings with price, address, sqft",
        method="spiral",
    )
    runner.run()

    # Resume after crash
    runner = FractalRunner.resume("scraper-realestate")
    runner.run()

    # CLI: openkeel fractal start "Build a scraper" --method spiral
    # CLI: openkeel fractal resume scraper-realestate
    # CLI: openkeel fractal status scraper-realestate
    # CLI: openkeel fractal list
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .engine import FractalEngine, FractalNode, FractalStatus, Resolution, Strategy
from .persistence import PersistentFractalEngine
from .methods import (
    FractalMethod, ZoomMethod, SwarmMethod, SpiralMethod, OrganismMethod,
)

log = logging.getLogger("openkeel.fractal.runner")


# ---------------------------------------------------------------------------
# Agent bridges — connect the fractal to real LLMs
# ---------------------------------------------------------------------------

FRACTAL_SYSTEM_PROMPT = """You are a fractal decomposition agent. You work on problems by recursively
breaking them down: build a rough version, test it, discover what's missing, decompose further.

When you respond, always include these JSON sections in your output:

```json
{
  "output": "description of what you built/did",
  "discoveries": ["thing I learned 1", "thing I learned 2"],
  "tests": [
    {"name": "test_name", "passed": true, "output": "details"},
    {"name": "another_test", "passed": false, "output": "what failed"}
  ],
  "subtasks": [
    {"title": "sub-problem 1", "description": "details"},
    {"title": "sub-problem 2", "description": "details"}
  ]
}
```

Only include "subtasks" if the problem needs decomposition.
Only include "tests" for things you actually tested.
Always include "discoveries" — what did you learn that you didn't know before?"""


def _parse_agent_response(raw: str) -> dict:
    """Extract structured data from an agent's response."""
    # Try JSON parse
    try:
        # Find JSON block in response
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1))

        # Try raw JSON
        brace_start = raw.find('{')
        brace_end = raw.rfind('}')
        if brace_start >= 0 and brace_end > brace_start:
            candidate = raw[brace_start:brace_end + 1]
            return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Fallback: extract what we can from free text
    result = {
        "output": raw[:500],
        "discoveries": [],
        "tests": [],
        "subtasks": [],
    }

    # Extract bullet points as discoveries
    for line in raw.split('\n'):
        line = line.strip()
        if line.startswith(('- ', '* ', '• ')) and len(line) > 10:
            result["discoveries"].append(line.lstrip('-*• ').strip())

    return result


def make_claude_agent(model: str = "sonnet") -> Callable:
    """Create an agent_fn that uses Claude CLI (claude -p).

    This runs `claude -p "instruction"` as a subprocess and parses
    the structured response.
    """
    def agent_fn(instruction: str, context: dict) -> dict:
        # Build the prompt with context
        prompt = f"{instruction}\n\nRespond with a JSON block containing: output, discoveries, tests, subtasks."

        if context.get("recovery"):
            prompt = f"RECOVERY CONTEXT:\n{context['recovery']}\n\n{prompt}"

        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--model", model],
                capture_output=True, text=True, timeout=300,
            )
            raw = result.stdout.strip()
            if not raw:
                raw = result.stderr.strip()
            return _parse_agent_response(raw)
        except FileNotFoundError:
            log.error("Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code")
            return {"output": "error: claude CLI not found", "discoveries": [], "tests": [], "subtasks": []}
        except subprocess.TimeoutExpired:
            return {"output": "error: agent timed out", "discoveries": ["Agent timed out after 5 minutes"], "tests": [], "subtasks": []}
        except Exception as e:
            return {"output": f"error: {e}", "discoveries": [], "tests": [], "subtasks": []}

    return agent_fn


def make_ollama_agent(model: str = "gemma4:e2b",
                      host: str = "127.0.0.1", port: int = 11434) -> Callable:
    """Create an agent_fn that uses a local Ollama model."""
    def agent_fn(instruction: str, context: dict) -> dict:
        import urllib.request

        prompt = instruction
        if context.get("recovery"):
            prompt = f"RECOVERY CONTEXT:\n{context['recovery']}\n\n{prompt}"

        try:
            data = json.dumps({
                "model": model,
                "system": FRACTAL_SYSTEM_PROMPT,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.4, "num_predict": 2048},
            }).encode()

            req = urllib.request.Request(
                f"http://{host}:{port}/api/generate",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=300)
            raw = json.loads(resp.read()).get("response", "")
            return _parse_agent_response(raw)
        except Exception as e:
            return {"output": f"error: {e}", "discoveries": [], "tests": [], "subtasks": []}

    return agent_fn


def make_callable_agent(fn: Callable[[str], str]) -> Callable:
    """Wrap any string→string function as an agent_fn."""
    def agent_fn(instruction: str, context: dict) -> dict:
        raw = fn(instruction)
        return _parse_agent_response(raw)
    return agent_fn


# ---------------------------------------------------------------------------
# Fractal Runner
# ---------------------------------------------------------------------------

class FractalRunner:
    """Drives the fractal decomposition loop.

    Connects a PersistentFractalEngine + a FractalMethod + an agent bridge.
    Handles: starting, running, pausing, resuming, and reporting.
    """

    def __init__(self, engine: PersistentFractalEngine, method: FractalMethod,
                 kanban=None, report_interval: int = 5):
        self.engine = engine
        self.method = method
        self.kanban = kanban
        self.report_interval = report_interval  # report to kanban every N iterations
        self._running = False
        self._paused = False
        self._iteration = 0

    @classmethod
    def start(cls, title: str, description: str = "",
              method: str = "spiral",
              agent: str = "claude",
              agent_model: str = "sonnet",
              kanban=None,
              fractal_id: str | None = None) -> "FractalRunner":
        """Start a new fractal job from scratch.

        Args:
            title: What to build
            description: Details
            method: "zoom", "swarm", "spiral", or "organism"
            agent: "claude", "ollama", or a callable
            agent_model: Model name for the agent backend
            kanban: Optional Kanban instance for task tracking
            fractal_id: Optional ID (auto-generated from title if omitted)
        """
        # Generate ID from title
        if not fractal_id:
            fractal_id = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:50]

        # Create engine
        engine = PersistentFractalEngine(fractal_id, kanban=kanban)

        # Create agent bridge
        agent_fn = _resolve_agent(agent, agent_model)

        # Map strategy
        strategy = Strategy(method)

        # Create root
        engine.create_root(title, description=description, strategy=strategy)

        # Create method
        method_obj = _resolve_method(method, engine, agent_fn)

        # Save initial state
        engine._remember(f"Fractal started: {title} [method={method}, agent={agent}]")

        runner = cls(engine, method_obj, kanban=kanban)
        log.info("Fractal runner started: '%s' [%s/%s] → %s",
                 title, method, agent, fractal_id)
        return runner

    @classmethod
    def resume(cls, fractal_id: str,
               method: str | None = None,
               agent: str = "claude",
               agent_model: str = "sonnet",
               kanban=None) -> "FractalRunner":
        """Resume a fractal after crash/reboot/new session.

        Loads state from disk, determines where we left off,
        and continues the loop.
        """
        engine = PersistentFractalEngine.resume(fractal_id, kanban=kanban)
        root = engine.get_root()

        if root is None:
            raise ValueError(f"Fractal '{fractal_id}' has no root node — cannot resume")

        # Use saved strategy or override
        if method is None:
            method = root.strategy

        agent_fn = _resolve_agent(agent, agent_model)
        method_obj = _resolve_method(method, engine, agent_fn)

        runner = cls(engine, method_obj, kanban=kanban)

        log.info("Fractal runner resumed: '%s' [%s] — %d nodes, %d%% complete",
                 root.title, method, len(engine._nodes),
                 engine.stats()["progress"])
        return runner

    def run(self, max_iterations: int = 50, callback: Callable | None = None) -> dict:
        """Run the fractal loop until completion, max iterations, or pause.

        Args:
            max_iterations: Safety limit
            callback: Optional fn(iteration, node, engine) called each cycle

        Returns:
            Execution summary dict
        """
        self._running = True
        self._paused = False
        start_time = time.time()

        root = self.engine.get_root()
        if not root:
            return {"error": "No root node"}

        log.info("Running fractal: '%s' (max %d iterations)", root.title, max_iterations)

        while self._running and self._iteration < max_iterations:
            if self._paused:
                time.sleep(0.5)
                continue

            # Pick next node
            node = self.method.next_node()
            if node is None:
                log.info("No more nodes to work on — fractal complete or blocked")
                break

            # Execute one cycle
            log.info("Iteration %d: node #%d '%s' [%s] depth=%d",
                     self._iteration, node.id, node.title, node.status, node.depth)

            try:
                self.method.execute(node)
            except Exception as e:
                log.error("Error executing node #%d: %s", node.id, e)
                self.engine.block(node.id, f"Execution error: {e}")

            self._iteration += 1

            # Periodic reporting
            if self._iteration % self.report_interval == 0:
                self._report_progress()

            # Callback
            if callback:
                try:
                    callback(self._iteration, node, self.engine)
                except Exception:
                    pass

            # Check if root is resolved
            root = self.engine.get_root()
            if root and root.status == FractalStatus.RESOLVED:
                log.info("ROOT RESOLVED at %s resolution after %d iterations",
                         root.resolution, self._iteration)
                break

        elapsed = time.time() - start_time
        self._running = False

        # Final report
        self._report_progress()

        stats = self.engine.stats()
        summary = {
            "fractal_id": self.engine.fractal_id,
            "title": root.title if root else "",
            "method": self.method.name,
            "iterations": self._iteration,
            "elapsed_seconds": round(elapsed, 1),
            "root_status": str(root.status) if root else "none",
            "root_resolution": str(root.resolution) if root else "none",
            "stats": stats,
        }

        # Save to Hyphae
        self.engine._remember(
            f"Fractal '{root.title}' {'completed' if root and root.status == FractalStatus.RESOLVED else 'paused'}: "
            f"{stats['resolved_count']}/{stats['total_nodes']} nodes, "
            f"{self._iteration} iterations, {round(elapsed)}s"
        )

        return summary

    def pause(self):
        """Pause the runner (state is already saved on disk)."""
        self._paused = True
        log.info("Fractal runner paused")

    def stop(self):
        """Stop the runner."""
        self._running = False
        log.info("Fractal runner stopped")

    def status(self) -> dict:
        """Get current runner status."""
        root = self.engine.get_root()
        return {
            "fractal_id": self.engine.fractal_id,
            "running": self._running,
            "paused": self._paused,
            "iteration": self._iteration,
            "root_title": root.title if root else "",
            "root_status": str(root.status) if root else "none",
            "stats": self.engine.stats(),
            "briefing": self.engine.get_briefing(),
        }

    def _report_progress(self):
        """Report progress to kanban board."""
        if not self.kanban:
            return

        root = self.engine.get_root()
        if not root or not root.task_id:
            return

        stats = self.engine.stats()
        try:
            # Update the root task description with progress
            report = (
                f"Fractal progress: {stats['progress']}% "
                f"({stats['resolved_count']}/{stats['total_nodes']} nodes, "
                f"depth {stats['max_depth']}, "
                f"{stats['total_discoveries']} discoveries)"
            )

            # Use kanban task report API if available
            import urllib.request
            data = json.dumps({
                "agent_name": "fractal-runner",
                "status": "done" if root.status == FractalStatus.RESOLVED else "in_progress",
                "report": report,
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:8200/api/task/{root.task_id}/report",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------

def cli_main(argv: list[str] | None = None) -> int:
    """CLI entry point for fractal operations.

    Usage:
        openkeel fractal start "Build a scraper" --method spiral --agent claude
        openkeel fractal resume scraper-id
        openkeel fractal status scraper-id
        openkeel fractal list
        openkeel fractal briefing scraper-id
        openkeel fractal tree scraper-id
    """
    import argparse

    parser = argparse.ArgumentParser(prog="openkeel fractal",
                                     description="Fractal task decomposition")
    sub = parser.add_subparsers(dest="command")

    # start
    p_start = sub.add_parser("start", help="Start a new fractal job")
    p_start.add_argument("title", help="What to build")
    p_start.add_argument("--description", "-d", default="", help="Details")
    p_start.add_argument("--method", "-m", default="spiral",
                         choices=["zoom", "swarm", "spiral", "organism"])
    p_start.add_argument("--agent", "-a", default="claude",
                         choices=["claude", "ollama"])
    p_start.add_argument("--model", default="sonnet", help="Agent model")
    p_start.add_argument("--max-iterations", "-n", type=int, default=50)
    p_start.add_argument("--id", default=None, help="Custom fractal ID")

    # resume
    p_resume = sub.add_parser("resume", help="Resume a fractal job")
    p_resume.add_argument("fractal_id", help="Fractal ID to resume")
    p_resume.add_argument("--method", "-m", default=None)
    p_resume.add_argument("--agent", "-a", default="claude")
    p_resume.add_argument("--model", default="sonnet")
    p_resume.add_argument("--max-iterations", "-n", type=int, default=50)

    # status
    p_status = sub.add_parser("status", help="Show fractal status")
    p_status.add_argument("fractal_id")

    # list
    sub.add_parser("list", help="List all fractals")

    # briefing
    p_brief = sub.add_parser("briefing", help="Show recovery briefing")
    p_brief.add_argument("fractal_id")

    # tree
    p_tree = sub.add_parser("tree", help="Show fractal tree")
    p_tree.add_argument("fractal_id")

    args = parser.parse_args(argv)

    if args.command == "start":
        runner = FractalRunner.start(
            title=args.title,
            description=args.description,
            method=args.method,
            agent=args.agent,
            agent_model=args.model,
            fractal_id=args.id,
        )
        print(f"Fractal started: {runner.engine.fractal_id}")
        print(f"  Method: {args.method}")
        print(f"  Agent: {args.agent}/{args.model}")
        print(f"  Max iterations: {args.max_iterations}")
        print()
        result = runner.run(max_iterations=args.max_iterations,
                            callback=_cli_callback)
        print()
        _print_summary(result)
        return 0

    elif args.command == "resume":
        runner = FractalRunner.resume(
            fractal_id=args.fractal_id,
            method=args.method,
            agent=args.agent,
            agent_model=args.model,
        )
        print(f"Resuming fractal: {args.fractal_id}")
        print()
        result = runner.run(max_iterations=args.max_iterations,
                            callback=_cli_callback)
        print()
        _print_summary(result)
        return 0

    elif args.command == "status":
        engine = PersistentFractalEngine.resume(args.fractal_id)
        stats = engine.stats()
        root = engine.get_root()
        print(f"Fractal: {args.fractal_id}")
        if root:
            print(f"  Title: {root.title}")
            print(f"  Status: {root.status}")
            print(f"  Resolution: {root.resolution}")
            print(f"  Strategy: {root.strategy}")
        print(f"  Nodes: {stats['total_nodes']}")
        print(f"  Resolved: {stats['resolved_count']}")
        print(f"  Progress: {stats['progress']}%")
        print(f"  Max depth: {stats['max_depth']}")
        print(f"  Discoveries: {stats['total_discoveries']}")
        active = engine.get_active()
        blocked = engine.get_blocked()
        if active:
            print(f"  Active: {len(active)} nodes need work")
        if blocked:
            print(f"  Blocked: {len(blocked)} nodes stuck")
        return 0

    elif args.command == "list":
        fractals = PersistentFractalEngine.list_fractals()
        if not fractals:
            print("No fractals found.")
            return 0
        print(f"{'ID':<40} {'Nodes':>6} {'Progress':>8} {'Last Saved'}")
        print("-" * 72)
        for f in fractals:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(f.get("last_saved", 0)))
            print(f"{f['id']:<40} {f.get('total_nodes', '?'):>6} {f.get('progress', '?'):>7}% {ts}")
        return 0

    elif args.command == "briefing":
        engine = PersistentFractalEngine.resume(args.fractal_id)
        print(engine.get_briefing())
        return 0

    elif args.command == "tree":
        engine = PersistentFractalEngine.resume(args.fractal_id)
        _print_tree(engine)
        return 0

    else:
        parser.print_help()
        return 1


def _cli_callback(iteration: int, node: FractalNode, engine: FractalEngine):
    """Print progress during CLI execution."""
    status_icon = {
        FractalStatus.EXPLORING: ">>",
        FractalStatus.TESTING: "??",
        FractalStatus.RESOLVED: "OK",
        FractalStatus.BLOCKED: "!!",
        FractalStatus.WAITING: "..",
        FractalStatus.INTEGRATING: "<>",
        FractalStatus.DECOMPOSING: "++",
    }.get(node.status, "  ")
    indent = "  " * node.depth
    disc_count = len(node.discoveries)
    print(f"  [{status_icon}] {indent}#{node.id} {node.title} "
          f"[{node.resolution}] ({disc_count} disc)")


def _print_summary(result: dict):
    """Print execution summary."""
    stats = result.get("stats", {})
    print(f"{'='*50}")
    print(f"Fractal: {result.get('fractal_id', '?')}")
    print(f"  Status: {result.get('root_status', '?')}")
    print(f"  Resolution: {result.get('root_resolution', '?')}")
    print(f"  Iterations: {result.get('iterations', 0)}")
    print(f"  Time: {result.get('elapsed_seconds', 0)}s")
    print(f"  Nodes: {stats.get('total_nodes', 0)} total, "
          f"{stats.get('resolved_count', 0)} resolved ({stats.get('progress', 0)}%)")
    print(f"  Discoveries: {stats.get('total_discoveries', 0)}")
    print(f"  Max depth: {stats.get('max_depth', 0)}")


def _print_tree(engine: FractalEngine, node: FractalNode | None = None, indent: int = 0):
    """Print the fractal tree to stdout."""
    if node is None:
        node = engine.get_root()
        if not node:
            print("(empty tree)")
            return

    status_icon = {
        FractalStatus.RESOLVED: "[x]",
        FractalStatus.BLOCKED: "[!]",
        FractalStatus.PRUNED: "[-]",
        FractalStatus.EXPLORING: "[>]",
        FractalStatus.WAITING: "[.]",
        FractalStatus.INTEGRATING: "[<]",
    }.get(node.status, "[ ]")

    strategy_tag = f" ({node.strategy})" if node.strategy != Strategy.ZOOM else ""
    res_tag = f" [{node.resolution}]" if node.resolution != Resolution.NONE else ""
    disc_tag = f" +{len(node.discoveries)}disc" if node.discoveries else ""

    prefix = "  " * indent
    print(f"{prefix}{status_icon} #{node.id} {node.title}{strategy_tag}{res_tag}{disc_tag}")

    for child_id in node.children:
        child = engine.get_node(child_id)
        if child:
            _print_tree(engine, child, indent + 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_agent(agent: str, model: str) -> Callable:
    """Resolve agent name to a callable agent_fn."""
    if agent == "claude":
        return make_claude_agent(model)
    elif agent == "ollama":
        return make_ollama_agent(model)
    elif callable(agent):
        return make_callable_agent(agent)
    else:
        raise ValueError(f"Unknown agent type: {agent}. Use 'claude' or 'ollama'.")


def _resolve_method(method: str | Strategy, engine: FractalEngine,
                    agent_fn: Callable) -> FractalMethod:
    """Resolve method name to a FractalMethod instance."""
    method_str = str(method)
    if method_str == "zoom":
        return ZoomMethod(engine, agent_fn)
    elif method_str == "swarm":
        return SwarmMethod(engine, agent_fn)
    elif method_str == "spiral":
        return SpiralMethod(engine, agent_fn)
    elif method_str == "organism":
        return OrganismMethod(engine, agent_fn)
    else:
        raise ValueError(f"Unknown method: {method}. Use zoom/swarm/spiral/organism.")
