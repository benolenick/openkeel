"""A/B battery test: vanilla vs bubble delegation configs.

Runs identical tasks under vanilla (Sonnet-does-everything) and flat (Sonnet+Haiku delegation),
measuring exact token usage, wall time, and cost.

Usage:
  python3 tests/ab_battery.py --repo ~/Desktop/openkeel2
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from openkeel.bubble.gather import haiku_api, get_repo_tree, TOOLS
from openkeel.bubble import ollama as local_llm
from openkeel.bubble.config import get_config

# ── Test tasks (increasing difficulty) ──────────────────────────

TASKS = [
    {
        "id": "T1",
        "name": "simple_lookup",
        "difficulty": "easy",
        "task": "What are the default settings values in the settings dialog? List each default and its value.",
    },
    {
        "id": "T2",
        "name": "cross_file_trace",
        "difficulty": "medium",
        "task": "Trace the data flow when a user clicks the 'Launch Claude' button. What functions are called, in what files, and what happens in the terminal?",
    },
    {
        "id": "T3",
        "name": "architecture_analysis",
        "difficulty": "medium",
        "task": "Explain the token tracking pipeline: how does OpenKeel detect and count tokens from a running Claude Code session? Trace from the JSONL file to the GUI dials.",
    },
    {
        "id": "T4",
        "name": "bug_hunt",
        "difficulty": "hard",
        "task": "Review the session_watcher.py and quota.py modules for potential bugs or edge cases. What happens if the JSONL file is deleted mid-session? What if two Claude sessions run simultaneously?",
    },
    {
        "id": "T5",
        "name": "feature_design",
        "difficulty": "hard",
        "task": "If we wanted to add a 'cost estimator' that predicts how much a task will cost before running it (based on repo size and task complexity), which modules would need changes and what's the minimal design?",
    },
]


# ── Tracking ────────────────────────────────────────────────────

class Tracker:
    def __init__(self):
        self.sonnet_calls = 0
        self.sonnet_wall_ms = 0
        self.haiku_calls = 0
        self.haiku_input_tok = 0
        self.haiku_output_tok = 0
        self.haiku_cost = 0.0
        self.local_calls = 0
        self.local_ms = 0

    def log_sonnet(self, wall_ms):
        self.sonnet_calls += 1
        self.sonnet_wall_ms += wall_ms

    def log_haiku(self, in_tok, out_tok, cost):
        self.haiku_calls += 1
        self.haiku_input_tok += in_tok
        self.haiku_output_tok += out_tok
        self.haiku_cost += cost

    def log_local(self, ms):
        self.local_calls += 1
        self.local_ms += ms

    def to_dict(self):
        return {
            "sonnet_calls": self.sonnet_calls,
            "sonnet_wall_ms": self.sonnet_wall_ms,
            "haiku_calls": self.haiku_calls,
            "haiku_input_tok": self.haiku_input_tok,
            "haiku_output_tok": self.haiku_output_tok,
            "haiku_cost": round(self.haiku_cost, 6),
            "local_calls": self.local_calls,
            "local_ms": self.local_ms,
        }


# ── Sonnet CLI ──────────────────────────────────────────────────

def call_sonnet(prompt, tracker):
    cfg = get_config()
    t0 = time.time()
    try:
        r = subprocess.run(
            [cfg["claude_bin"], "-p", "--model", cfg["reason_model"],
             "--no-session-persistence", "--output-format", "text", prompt],
            capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
        )
        ms = int((time.time() - t0) * 1000)
        tracker.log_sonnet(ms)
        return r.stdout.strip()
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        tracker.log_sonnet(ms)
        return f"[error: {e}]"


def call_haiku_api(prompt, system, tracker, tools=None, max_rounds=1):
    text, in_tok, out_tok, cost = haiku_api(
        prompt, system, max_tokens=1024, max_rounds=max_rounds, tools=tools,
    )
    tracker.log_haiku(in_tok, out_tok, cost)
    return text


def call_local_llm(prompt, model, tracker, max_tokens=1024):
    text, elapsed = local_llm.generate(prompt, model, max_tokens=max_tokens)
    tracker.log_local(elapsed)
    return text


# ── Configs ─────────────────────────────────────────────────────

def run_vanilla(task_text, repo_path, repo_tree, tracker):
    """Vanilla: Sonnet does plan + execute + synthesize. All Sonnet."""

    # Plan
    plan = call_sonnet(
        f"Plan how to analyze this codebase task. List 3-4 specific sub-tasks.\n\n"
        f"Files:\n{repo_tree}\n\nTask: {task_text}\n\n"
        f"Reply with numbered sub-tasks.",
        tracker,
    )

    sub_tasks = _parse_subtasks(plan, task_text)

    # Execute each sub-task (Sonnet reads files and answers)
    gathered = ""
    for st in sub_tasks[:4]:
        answer = call_sonnet(
            f"Analyze code in the repo.\n\nPrevious findings:\n{gathered[-2000:]}\n\n"
            f"Task: {st}\n\nFiles:\n{repo_tree}\n\nRead relevant files and answer. Cite file paths.",
            tracker,
        )
        gathered += f"\n\n## {st}\n{answer[:2000]}"

    # Synthesize
    final = call_sonnet(
        f"Synthesize findings into a final answer.\n\nOriginal task: {task_text}\n\n"
        f"Findings:\n{gathered[:10000]}\n\nProvide comprehensive answer with file paths.",
        tracker,
    )
    return final


def run_flat(task_text, repo_path, repo_tree, local_model, tracker):
    """Flat: Sonnet plans + synthesizes. Haiku classifies, routes to Haiku or local for execution."""

    # Plan (Sonnet)
    plan = call_sonnet(
        f"Plan how to analyze this codebase task. List 3-4 specific sub-tasks.\n\n"
        f"Files:\n{repo_tree}\n\nTask: {task_text}\n\n"
        f"Reply with numbered sub-tasks.",
        tracker,
    )

    sub_tasks = _parse_subtasks(plan, task_text)

    # Execute each sub-task via delegation
    gathered = ""
    for st in sub_tasks[:4]:
        # Haiku classifies
        classify = call_haiku_api(
            f'Can a small 5B model answer this? Reply JSON: {{"local_capable": true/false}}\n\n'
            f'true = single-file read, value lookup\nfalse = cross-file analysis, reasoning\n\n'
            f'Task: {st[:300]}',
            system='Reply with only JSON.',
            tracker=tracker,
        )

        import re
        local_ok = False
        try:
            m = re.search(r'\{[^}]+\}', classify or "")
            if m:
                local_ok = json.loads(m.group()).get("local_capable", False)
        except Exception:
            pass

        if local_ok and local_model:
            # Local gathers + answers
            from openkeel.bubble.gather import _plan_local, validate_commands
            commands, _ = _plan_local(st, repo_tree, "", local_model)
            commands = validate_commands(commands, repo_path)
            data = ""
            for cmd in commands[:4]:
                try:
                    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                    data += f"\n### {cmd}\n{r.stdout[:3000]}"
                except Exception:
                    pass
            answer = call_local_llm(
                f"Task: {st}\n\nData:\n{data[:6000]}\n\nAnswer concisely.",
                local_model, tracker,
            )
        else:
            # Haiku handles with tool use
            answer = call_haiku_api(
                f"Analyze code in {repo_path}.\nFiles:\n{repo_tree}\n\nTask: {st}\n\nUse tools to read files and answer.",
                system=f"Read files and answer. Be concise.",
                tracker=tracker, tools=TOOLS, max_rounds=3,
            )

        gathered += f"\n\n## {st}\n{answer[:2000]}"

    # Synthesize (Sonnet)
    final = call_sonnet(
        f"Synthesize findings into a final answer.\n\nOriginal task: {task_text}\n\n"
        f"Findings:\n{gathered[:10000]}\n\nProvide comprehensive answer with file paths.",
        tracker,
    )
    return final


# ── Helpers ─────────────────────────────────────────────────────

def _parse_subtasks(plan, fallback):
    tasks = []
    for line in plan.split("\n"):
        line = line.strip()
        if line and line[0].isdigit() and ("." in line[:4] or ")" in line[:4]):
            text = line.split(".", 1)[-1].strip() if "." in line[:4] else line.split(")", 1)[-1].strip()
            if text:
                tasks.append(text)
    return tasks if tasks else [fallback]


# ── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="A/B battery: vanilla vs bubble")
    parser.add_argument("--repo", required=True, help="Repository path to analyze")
    parser.add_argument("--local-model", default="gemma4:e2b")
    parser.add_argument("--tasks", default="all", help="Comma-separated task IDs (T1,T2,...) or 'all'")
    parser.add_argument("--output", default="ab_results.json")
    args = parser.parse_args()

    repo_tree = get_repo_tree(args.repo)
    print(f"Repo: {args.repo} ({len(repo_tree.splitlines())} files)", file=sys.stderr)

    # Filter tasks
    if args.tasks == "all":
        tasks = TASKS
    else:
        ids = set(args.tasks.split(","))
        tasks = [t for t in TASKS if t["id"] in ids]

    results = []
    totals = {"vanilla": Tracker(), "flat": Tracker()}

    for task in tasks:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"TASK {task['id']}: {task['name']} ({task['difficulty']})", file=sys.stderr)
        print(f"  {task['task'][:80]}...", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)

        entry = {"task_id": task["id"], "name": task["name"], "difficulty": task["difficulty"]}

        # ── Vanilla ──
        print(f"\n  [VANILLA] Running...", file=sys.stderr)
        v_tracker = Tracker()
        t0 = time.time()
        v_output = run_vanilla(task["task"], args.repo, repo_tree, v_tracker)
        v_wall = int((time.time() - t0) * 1000)
        entry["vanilla"] = {**v_tracker.to_dict(), "wall_ms": v_wall, "output_len": len(v_output)}
        print(f"  [VANILLA] Done: {v_tracker.sonnet_calls} Sonnet calls, {v_wall/1000:.1f}s", file=sys.stderr)

        # Accumulate totals
        totals["vanilla"].sonnet_calls += v_tracker.sonnet_calls
        totals["vanilla"].sonnet_wall_ms += v_tracker.sonnet_wall_ms

        # ── Flat (bubble) ──
        print(f"\n  [FLAT] Running...", file=sys.stderr)
        f_tracker = Tracker()
        t0 = time.time()
        f_output = run_flat(task["task"], args.repo, repo_tree, args.local_model, f_tracker)
        f_wall = int((time.time() - t0) * 1000)
        entry["flat"] = {**f_tracker.to_dict(), "wall_ms": f_wall, "output_len": len(f_output)}
        print(f"  [FLAT] Done: {f_tracker.sonnet_calls} Sonnet, {f_tracker.haiku_calls} Haiku, "
              f"{f_tracker.local_calls} Local, {f_wall/1000:.1f}s", file=sys.stderr)

        # Accumulate totals
        totals["flat"].sonnet_calls += f_tracker.sonnet_calls
        totals["flat"].sonnet_wall_ms += f_tracker.sonnet_wall_ms
        totals["flat"].haiku_calls += f_tracker.haiku_calls
        totals["flat"].haiku_input_tok += f_tracker.haiku_input_tok
        totals["flat"].haiku_output_tok += f_tracker.haiku_output_tok
        totals["flat"].haiku_cost += f_tracker.haiku_cost
        totals["flat"].local_calls += f_tracker.local_calls
        totals["flat"].local_ms += f_tracker.local_ms

        # Reduction
        if v_tracker.sonnet_calls > 0:
            reduction = (1 - f_tracker.sonnet_calls / v_tracker.sonnet_calls) * 100
            entry["sonnet_reduction_pct"] = round(reduction, 1)
            print(f"\n  >> Sonnet reduction: {reduction:.1f}%", file=sys.stderr)

        results.append(entry)

    # ── Summary ──────────────────────────────────────────────────
    print(f"\n\n{'='*60}", file=sys.stderr)
    print(f"AGGREGATE RESULTS ({len(tasks)} tasks)", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    v_total = totals["vanilla"].sonnet_calls
    f_total = totals["flat"].sonnet_calls
    reduction = (1 - f_total / max(v_total, 1)) * 100

    print(f"\n  VANILLA total Sonnet calls: {v_total}", file=sys.stderr)
    print(f"  FLAT total Sonnet calls:    {f_total}", file=sys.stderr)
    print(f"  Sonnet reduction:           {reduction:.1f}%", file=sys.stderr)
    print(f"\n  FLAT Haiku calls:           {totals['flat'].haiku_calls}", file=sys.stderr)
    print(f"  FLAT Haiku tokens:          {totals['flat'].haiku_input_tok + totals['flat'].haiku_output_tok:,}", file=sys.stderr)
    print(f"  FLAT Haiku cost:            ${totals['flat'].haiku_cost:.4f}", file=sys.stderr)
    print(f"  FLAT Local calls:           {totals['flat'].local_calls}", file=sys.stderr)

    # OEQ comparison (each Sonnet CLI call ~ 2600 OEQ)
    v_oeq = v_total * 2600
    f_oeq = f_total * 2600  # only Sonnet calls count against quota
    print(f"\n  VANILLA OEQ burn:           {v_oeq:,}", file=sys.stderr)
    print(f"  FLAT OEQ burn:              {f_oeq:,}", file=sys.stderr)
    print(f"  OEQ saved:                  {v_oeq - f_oeq:,} ({reduction:.1f}%)", file=sys.stderr)

    summary = {
        "tasks_run": len(tasks),
        "vanilla_sonnet_calls": v_total,
        "flat_sonnet_calls": f_total,
        "sonnet_reduction_pct": round(reduction, 1),
        "flat_haiku_calls": totals["flat"].haiku_calls,
        "flat_haiku_tokens": totals["flat"].haiku_input_tok + totals["flat"].haiku_output_tok,
        "flat_haiku_cost": round(totals["flat"].haiku_cost, 4),
        "flat_local_calls": totals["flat"].local_calls,
        "vanilla_oeq": v_oeq,
        "flat_oeq": f_oeq,
        "oeq_saved": v_oeq - f_oeq,
        "results": results,
    }

    # Save
    out_path = Path(args.output)
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"\n  Results saved to: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
