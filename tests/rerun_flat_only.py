"""Re-run only flat mode with stdin fix, merge with v2 vanilla data."""
import json
import subprocess
import sys
import time
import os
import re

sys.path.insert(0, '/home/om/Desktop/openkeel2/src')
os.chdir('/home/om/Desktop/openkeel2')

from openkeel.bubble.gather import gather, get_repo_tree, TOOLS
from openkeel.bubble import ollama as local_llm

# Load v2 results to get vanilla baselines + task definitions
with open('tests/ab_results_v2.json') as f:
    v2 = json.load(f)

v2_results = v2["results"]

# Get unique tasks (skip repeats for now)
tasks = {}
for entry in v2_results:
    tid = entry['task_id']; k = tid
    if entry.get('is_repeat'):
        continue
    tasks[tid] = {
        'name': entry['name'],
        'difficulty': entry['difficulty'],
        'task': entry['task'],
        'vanilla': entry.get('vanilla', {}),
    }

print(f"Loaded {len(tasks)} unique tasks from v2", file=sys.stderr)

# Config
CLAUDE_BIN = "claude"
REASON_MODEL = "sonnet"
LOCAL_MODEL = "gemma4:e2b"
REPO = "/home/om/Desktop/openkeel2"

repo_tree = get_repo_tree(REPO)
print(f"Repo tree: {len(repo_tree.splitlines())} files", file=sys.stderr)


def call_sonnet_fixed(prompt):
    """Call Sonnet via stdin (the fix)."""
    t0 = time.time()
    r = subprocess.run(
        [CLAUDE_BIN, "-p", "--model", REASON_MODEL,
         "--no-session-persistence", "--output-format", "json"],
        capture_output=True, text=True, timeout=300, input=prompt,
    )
    ms = int((time.time() - t0) * 1000)
    try:
        data = json.loads(r.stdout)
        usage = data.get("usage", {})
        return (
            data.get("result", "").strip(),
            ms,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            usage.get("cache_creation_input_tokens", 0),
            usage.get("cache_read_input_tokens", 0),
            data.get("total_cost_usd", 0.0),
        )
    except (json.JSONDecodeError, KeyError):
        return r.stdout.strip(), ms, 0, 0, 0, 0, 0.0


def call_haiku(prompt, system, tools=None, max_rounds=3):
    """Call Haiku via API (same as test)."""
    from openkeel.bubble.gather import haiku_api
    text, in_tok, out_tok, cost = haiku_api(
        prompt, system, max_tokens=1024, max_rounds=max_rounds, tools=tools
    )
    return text, in_tok, out_tok, cost


def run_flat_fixed(task_text):
    """Run flat mode with the stdin fix."""
    sonnet_calls = 0
    sonnet_in = 0; sonnet_out = 0; sonnet_cache_c = 0; sonnet_cache_r = 0; sonnet_cost = 0.0; sonnet_wall = 0
    haiku_calls = 0; haiku_in = 0; haiku_out = 0; haiku_cost_total = 0.0
    local_calls = 0; local_in = 0; local_out = 0; local_ms = 0

    # Plan (Sonnet call 1)
    plan_text, ms, s_in, s_out, cc, cr, cost = call_sonnet_fixed(
        f"Plan how to analyze this codebase task. List 3-4 specific sub-tasks.\n\n"
        f"Files:\n{repo_tree}\n\nTask: {task_text}\n\nReply with numbered sub-tasks."
    )
    sonnet_calls += 1; sonnet_in += s_in; sonnet_out += s_out
    sonnet_cache_c += cc; sonnet_cache_r += cr; sonnet_cost += cost; sonnet_wall += ms

    # Parse sub-tasks
    sub_tasks = []
    for line in plan_text.split("\n"):
        line = line.strip()
        if line and line[0].isdigit() and ("." in line[:4] or ")" in line[:4]):
            text = line.split(".", 1)[-1].strip() if "." in line[:4] else line.split(")", 1)[-1].strip()
            if text:
                sub_tasks.append(text)
    if not sub_tasks:
        sub_tasks = [task_text]

    # Execute sub-tasks via Haiku/Local
    gathered = ""
    for st in sub_tasks[:4]:
        # Classify
        classify, c_in, c_out, c_cost = call_haiku(
            f'Can a small 5B model answer this? Reply JSON: {{"local_capable": true/false}}\n\n'
            f'true = single-file read, value lookup\nfalse = cross-file analysis, reasoning\n\n'
            f'Task: {st[:300]}',
            system='Reply with only JSON.',
        )
        haiku_calls += 1; haiku_in += c_in; haiku_out += c_out; haiku_cost_total += c_cost

        local_ok = False
        try:
            m = re.search(r'\{[^}]+\}', classify or "")
            if m:
                local_ok = json.loads(m.group()).get("local_capable", False)
        except Exception:
            pass

        if local_ok:
            from openkeel.bubble.gather import _plan_local, validate_commands
            commands, plan_ms = _plan_local(st, repo_tree, "", LOCAL_MODEL)
            commands = validate_commands(commands, REPO)
            data = ""
            for cmd in commands[:4]:
                try:
                    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                    data += f"\n### {cmd}\n{r.stdout[:3000]}"
                except Exception:
                    pass
            answer, elapsed, li, lo = local_llm.generate_with_usage(
                f"Task: {st}\n\nData:\n{data[:6000]}\n\nAnswer concisely.",
                LOCAL_MODEL, max_tokens=1024,
            )
            local_calls += 1; local_in += li; local_out += lo; local_ms += elapsed
        else:
            answer, h_in, h_out, h_cost = call_haiku(
                f"Analyze code in {REPO}.\nFiles:\n{repo_tree}\n\n"
                f"Task: {st}\n\nUse tools to read files and answer.",
                system="Read files and answer. Be concise.",
                tools=TOOLS, max_rounds=3,
            )
            haiku_calls += 1; haiku_in += h_in; haiku_out += h_out; haiku_cost_total += h_cost

        gathered += f"\n\n## {st}\n{answer[:2000]}"

    # Synthesize (Sonnet call 2) — THE FIX: via stdin
    synth_text, ms, s_in, s_out, cc, cr, cost = call_sonnet_fixed(
        f"Synthesize findings into a final answer.\n\nOriginal task: {task_text}\n\n"
        f"Findings:\n{gathered[:10000]}\n\nProvide comprehensive answer with file paths."
    )
    sonnet_calls += 1; sonnet_in += s_in; sonnet_out += s_out
    sonnet_cache_c += cc; sonnet_cache_r += cr; sonnet_cost += cost; sonnet_wall += ms

    return synth_text, {
        "sonnet_calls": sonnet_calls,
        "sonnet_wall_ms": sonnet_wall,
        "sonnet_input_tok": sonnet_in,
        "sonnet_output_tok": sonnet_out,
        "sonnet_cache_create_tok": sonnet_cache_c,
        "sonnet_cache_read_tok": sonnet_cache_r,
        "sonnet_cost_usd": sonnet_cost,
        "haiku_calls": haiku_calls,
        "haiku_input_tok": haiku_in,
        "haiku_output_tok": haiku_out,
        "haiku_cost": haiku_cost_total,
        "local_calls": local_calls,
        "local_ms": local_ms,
        "local_input_tok": local_in,
        "local_output_tok": local_out,
    }


# Run flat mode for all tasks
results = {}
t_start = time.time()
for i, (tid, task) in enumerate(sorted(tasks.items())):
    print(f"\n[{i+1}/{len(tasks)}] {tid} {task['name']} — flat (stdin fix)", file=sys.stderr)
    t0 = time.time()
    output, metrics = run_flat_fixed(task['task'])
    wall = int((time.time() - t0) * 1000)
    metrics['wall_ms'] = wall
    metrics['output_len'] = len(output)

    results[tid] = {
        'task_id': tid,
        'name': task['name'],
        'difficulty': task['difficulty'],
        'task': task['task'],
        'vanilla': task['vanilla'],
        'flat': metrics,
        'flat_output': output,
        'vanilla_output': '',  # don't have it, but vanilla scores are from v2
    }
    print(f"  {metrics['sonnet_calls']} Sonnet (in={metrics['sonnet_input_tok']:,} out={metrics['sonnet_output_tok']:,}), "
          f"{metrics['haiku_calls']} Haiku, {metrics['local_calls']} Local, {wall//1000}s", file=sys.stderr)

    # Save incremental
    with open('tests/ab_results_v3_flat.json', 'w') as f:
        json.dump({"status": "in_progress", "completed": results,
                   "elapsed_ms": int((time.time() - t_start) * 1000)}, f, indent=2)

total_wall = int((time.time() - t_start) * 1000)

# Final save
with open('tests/ab_results_v3_flat.json', 'w') as f:
    json.dump({"status": "done", "completed": results,
               "elapsed_ms": total_wall}, f, indent=2)

print(f"\n{'='*60}", file=sys.stderr)
print(f"FLAT-ONLY RE-RUN COMPLETE ({len(results)} tasks, {total_wall/60000:.1f} min)", file=sys.stderr)

# Quick summary
total_s_in_v2 = sum(t['vanilla'].get('sonnet_input_tok',0) for t in results.values())
total_s_out_v2 = sum(t['vanilla'].get('sonnet_output_tok',0) for t in results.values())
total_s_in_v3 = sum(t['flat']['sonnet_input_tok'] for t in results.values())
total_s_out_v3 = sum(t['flat']['sonnet_output_tok'] for t in results.values())
print(f"Vanilla Sonnet tokens (from v2): {total_s_in_v2+total_s_out_v2:,}", file=sys.stderr)
print(f"Flat Sonnet tokens (v3 fixed):   {total_s_in_v3+total_s_out_v3:,}", file=sys.stderr)
print(f"{'='*60}", file=sys.stderr)
