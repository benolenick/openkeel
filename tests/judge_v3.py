"""Judge v3 flat outputs against v2 vanilla baselines."""
import json
import subprocess
import random
import re
import sys
import time

# Load v3 flat results (has flat_output stored)
with open('/home/om/Desktop/openkeel2/tests/ab_results_v3_flat.json') as f:
    v3 = json.load(f)

# Load v2 results for vanilla baselines
with open('/home/om/Desktop/openkeel2/tests/ab_results_v2.json') as f:
    v2 = json.load(f)

# We need vanilla outputs — but they were stripped from the JSON.
# However, v3 has flat_output. We need to re-run vanilla to get outputs...
# Actually, v3 completed dict has 'flat_output' field. Let's check.
v3_tasks = v3['completed']

# Check if flat_output exists
sample = list(v3_tasks.values())[0]
has_flat_output = 'flat_output' in sample
print(f"Has flat_output: {has_flat_output}", file=sys.stderr)
if has_flat_output:
    print(f"Sample flat_output length: {len(sample.get('flat_output',''))}", file=sys.stderr)

# We don't have vanilla_output in v3 (we reused v2 vanilla metrics only).
# But v2 also stripped outputs. So we need to generate vanilla outputs fresh.
# HOWEVER — we can just judge the flat outputs by themselves on absolute quality,
# or we can re-run vanilla for the key tasks.
# 
# Better approach: run each v3 flat output through the judge prompt standalone,
# asking "rate this response 1-10 on accuracy, completeness, specificity"
# Then compare to v2's vanilla scores.

JUDGE_PROMPT = """You are evaluating two code analysis responses for the same task. Rate each 1-10.

Task: {task}

=== Response A ===
{output_a}

=== Response B ===
{output_b}

Rate each on:
- accuracy (1-10): Are claims factually correct? File paths real?
- completeness (1-10): Does it answer all parts?
- specificity (1-10): Does it cite files, line numbers, functions?
- overall (1-10): Holistic quality.

Reply with ONLY JSON:
{{"a": {{"accuracy": N, "completeness": N, "specificity": N, "overall": N}}, "b": {{"accuracy": N, "completeness": N, "specificity": N, "overall": N}}}}"""

# We need vanilla outputs. Let's regenerate them for the tasks that matter most.
# Actually — the cleanest approach: run vanilla fresh on the 4 tasks flat lost on,
# plus hyphae_integration, and judge against v3 flat.

# Key tasks to judge (the ones where vanilla beat flat in v2):
KEY_TASKS = ['T11', 'T2', 'T14', 'T5']  # hyphae, launch_trace, failure_modes, cost_estimator
# Plus a few flat-wins for balance:
BALANCE_TASKS = ['T1', 'T13', 'T3', 'T7']

ALL_JUDGE = KEY_TASKS + BALANCE_TASKS

rng = random.Random(99)

results = {}
for tid in ALL_JUDGE:
    if tid not in v3_tasks:
        print(f"Skipping {tid} — not in v3", file=sys.stderr)
        continue
    
    entry = v3_tasks[tid]
    task_text = entry['task']
    flat_output = entry.get('flat_output', '')
    
    if not flat_output:
        print(f"Skipping {tid} — no flat_output", file=sys.stderr)
        continue
    
    # Generate vanilla output fresh
    print(f"[{tid}] {entry['name']} — generating vanilla...", file=sys.stderr)
    t0 = time.time()
    r = subprocess.run(
        ['claude', '-p', '--model', 'sonnet', '--no-session-persistence', '--output-format', 'json',
         f"Analyze this codebase. Task: {task_text}\n\nProvide a detailed answer with exact file paths and line numbers."],
        capture_output=True, text=True, timeout=300, input=task_text,
    )
    try:
        d = json.loads(r.stdout)
        vanilla_output = d.get('result', '')
    except:
        vanilla_output = r.stdout.strip()
    
    v_ms = int((time.time() - t0) * 1000)
    print(f"  vanilla: {len(vanilla_output)} chars, {v_ms}ms", file=sys.stderr)
    
    # Blind assignment
    if rng.random() < 0.5:
        a_is_vanilla = True
        output_a, output_b = vanilla_output, flat_output
    else:
        a_is_vanilla = False
        output_a, output_b = flat_output, vanilla_output
    
    # Judge
    print(f"  judging...", file=sys.stderr)
    prompt = JUDGE_PROMPT.format(
        task=task_text, output_a=output_a[:4000], output_b=output_b[:4000]
    )
    r = subprocess.run(
        ['claude', '-p', '--no-session-persistence', '--output-format', 'text'],
        capture_output=True, text=True, timeout=120, input=prompt,
    )
    
    try:
        m = re.search(r'\{.*\}', r.stdout, re.DOTALL)
        if m:
            scores = json.loads(m.group())
            v_key = 'a' if a_is_vanilla else 'b'
            f_key = 'b' if a_is_vanilla else 'a'
            v_scores = scores.get(v_key, {})
            f_scores = scores.get(f_key, {})
            
            results[tid] = {
                'name': entry['name'],
                'difficulty': entry['difficulty'],
                'vanilla_overall': v_scores.get('overall', 0),
                'flat_overall': f_scores.get('overall', 0),
                'vanilla_accuracy': v_scores.get('accuracy', 0),
                'flat_accuracy': f_scores.get('accuracy', 0),
                'vanilla_completeness': v_scores.get('completeness', 0),
                'flat_completeness': f_scores.get('completeness', 0),
                'vanilla_specificity': v_scores.get('specificity', 0),
                'flat_specificity': f_scores.get('specificity', 0),
                'v2_vanilla_overall': 0,  # filled below
                'v2_flat_overall': 0,
            }
            print(f"  V={v_scores.get('overall','?')} F={f_scores.get('overall','?')}", file=sys.stderr)
    except Exception as e:
        print(f"  judge parse error: {e}", file=sys.stderr)

# Get v2 scores for comparison
for r in v2['results']:
    tid = r['task_id']
    if tid in results and 'quality' in r:
        q = r['quality']
        v2c = q.get('claude', {})
        results[tid]['v2_vanilla_overall'] = v2c.get('vanilla', {}).get('overall', 0)
        results[tid]['v2_flat_overall'] = v2c.get('flat', {}).get('overall', 0)

# Print results
print("\n" + "=" * 80)
print("QUALITY COMPARISON: v2 (broken) vs v3 (stdin fix)")
print("=" * 80)
print(f"{'Task':<28} {'Diff':>5} | {'v2 Van':>6} {'v2 Flat':>7} {'v2 Win':>7} | {'v3 Van':>6} {'v3 Flat':>7} {'v3 Win':>7}")
print("-" * 80)

v3_v_total = 0; v3_f_total = 0; n = 0
for tid in ALL_JUDGE:
    if tid not in results: continue
    r = results[tid]
    n += 1
    v2_win = 'VAN' if r['v2_vanilla_overall'] > r['v2_flat_overall'] else ('FLAT' if r['v2_flat_overall'] > r['v2_vanilla_overall'] else 'TIE')
    v3_win = 'VAN' if r['vanilla_overall'] > r['flat_overall'] else ('FLAT' if r['flat_overall'] > r['vanilla_overall'] else 'TIE')
    
    v3_v_total += r['vanilla_overall']
    v3_f_total += r['flat_overall']
    
    flag = ' ***' if tid == 'T11' else ''
    print(f"{r['name']:<28} {r['difficulty']:>5} | {r['v2_vanilla_overall']:>6} {r['v2_flat_overall']:>7} {v2_win:>7} | {r['vanilla_overall']:>6} {r['flat_overall']:>7} {v3_win:>7}{flag}")

print("-" * 80)
if n > 0:
    print(f"{'MEAN':>28} {'':>5} | {'':>6} {'':>7} {'':>7} | {v3_v_total/n:>6.1f} {v3_f_total/n:>7.1f}")

print("\nDone.")
