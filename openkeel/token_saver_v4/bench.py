"""Token Saver v4 benchmark harness.

Reads real events from ~/.openkeel/token_ledger.db, replays file_read,
grep_output, file_write, bash_output, and session_start blobs through
the v4 lingua_compressor, and reports additional savings on top of what
v3 already caught.

Does NOT call live APIs. Pure measurement on historical data.

Usage:
    python -m openkeel.token_saver_v4.bench [--limit N] [--event-type X]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# Make import work whether run as module or script
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from openkeel.token_saver_v4.engines import lingua_compressor
from openkeel.token_saver_v4.engines import subagent_offload
from openkeel.token_saver_v4.engines import recall_rerank
from openkeel.token_saver_v4.engines import diff_compressor
from openkeel.token_saver_v4.engines import error_distiller
from openkeel.token_saver_v4.engines import webfetch_summarizer
from openkeel.token_saver_v4.engines import pre_compactor
from openkeel.token_saver_v4.engines import goal_reader
from openkeel.token_saver_v4.engines import subagent_filter

LEDGER = Path(os.path.expanduser("~/.openkeel/token_ledger.db"))

# Events worth compressing — ones where v3 currently records 0 or partial savings
TARGET_EVENTS = (
    "file_read",
    "grep_output",
    "file_write",
    "bash_output",
    "glob_output",
    "session_start",
)


def fetch_samples(limit_per_event: int = 200) -> dict[str, list[dict]]:
    """Pull a sample of notes for each target event type.

    The ledger doesn't store full blob contents (only char counts), so we
    synthesize realistic blobs from the `notes` field where available and
    fall back to a char-count-only measurement for events without notes.
    """
    conn = sqlite3.connect(str(LEDGER))
    samples: dict[str, list[dict]] = {}
    for evt in TARGET_EVENTS:
        rows = conn.execute(
            "SELECT original_chars, saved_chars, notes, file_path "
            "FROM savings WHERE event_type = ? "
            "AND original_chars > 0 "
            "ORDER BY original_chars DESC LIMIT ?",
            (evt, limit_per_event),
        ).fetchall()
        samples[evt] = [
            {
                "original_chars": r[0],
                "saved_chars_v3": r[1] or 0,
                "notes": r[2] or "",
                "file_path": r[3] or "",
            }
            for r in rows
        ]
    conn.close()
    return samples


def _build_probe_blob(sample: dict) -> str:
    """Build a test blob from real sources to feed into the v4 compressor.

    Strategy: if file_path exists and we can read it, use real content.
    Otherwise synthesize a representative blob from notes (capped).
    """
    fp = sample.get("file_path")
    if fp and os.path.exists(fp):
        try:
            with open(fp, "r", errors="replace") as f:
                content = f.read(sample["original_chars"] + 1000)
            if len(content) >= 200:
                return content
        except Exception:
            pass
    # Fallback: inflate notes to original_chars with repetition of a known
    # boilerplate pattern. Not ideal but measurable.
    notes = sample.get("notes") or "log line"
    target = max(500, sample["original_chars"])
    pad = (notes + "\n") * (1 + target // max(1, len(notes)))
    return pad[:target]


def bench_lingua() -> dict:
    samples = fetch_samples()
    totals = {
        "events": 0,
        "original_chars": 0,
        "v3_saved_chars": 0,
        "v4_additional_chars": 0,
        "by_event": {},
    }
    for evt, rows in samples.items():
        ev_total = {
            "count": len(rows),
            "original": 0,
            "v3_saved": 0,
            "v4_additional": 0,
            "samples_tested": 0,
        }
        for sample in rows[:50]:  # cap work
            blob = _build_probe_blob(sample)
            if len(blob) < lingua_compressor.MIN_CHARS:
                continue
            # Simulate v3 state: the blob after v3 already ran
            # (v3 savings were already applied; v4 operates on what remains)
            v3_remaining = max(
                lingua_compressor.MIN_CHARS,
                len(blob) - sample["saved_chars_v3"],
            )
            v3_blob = blob[:v3_remaining] if v3_remaining < len(blob) else blob

            result = lingua_compressor.compress(v3_blob)
            ev_total["samples_tested"] += 1
            ev_total["original"] += sample["original_chars"]
            ev_total["v3_saved"] += sample["saved_chars_v3"]
            ev_total["v4_additional"] += result.saved_chars

        totals["events"] += ev_total["count"]
        totals["original_chars"] += ev_total["original"]
        totals["v3_saved_chars"] += ev_total["v3_saved"]
        totals["v4_additional_chars"] += ev_total["v4_additional"]
        totals["by_event"][evt] = ev_total
    return totals


def bench_subagent_offload() -> dict:
    """Replay session tool sequences from the ledger and count nudge opportunities."""
    if not LEDGER.exists():
        return {"error": "no ledger"}
    conn = sqlite3.connect(str(LEDGER))
    rows = conn.execute(
        "SELECT session_id, timestamp, event_type "
        "FROM savings ORDER BY session_id, timestamp"
    ).fetchall()
    conn.close()

    # Map event_type back to tool names (approximate)
    evt_to_tool = {
        "file_read": "Read",
        "grep_output": "Grep",
        "glob_output": "Glob",
        "file_edit": "Edit",
        "file_write": "Write",
        "bash_output": "Bash",
    }

    sessions: dict[str, list[str]] = {}
    for sid, _ts, evt in rows:
        tool = evt_to_tool.get(evt)
        if tool:
            sessions.setdefault(sid, []).append(tool)

    nudges = 0
    total_chains = 0
    for sid, tools in sessions.items():
        # Slide through the session, firing nudges with cooldown
        events_since = 999
        for i in range(len(tools)):
            decision = subagent_offload.evaluate(
                tools[: i + 1], events_since_last_nudge=events_since
            )
            events_since += 1
            if decision.should_nudge:
                nudges += 1
                total_chains += decision.chain_len
                events_since = 0
    return {
        "sessions_analyzed": len(sessions),
        "nudges_fired": nudges,
        "avg_chain_len": round(total_chains / nudges, 2) if nudges else 0,
        "avg_nudges_per_session": round(nudges / max(1, len(sessions)), 3),
    }


def _probe_recall_queries() -> list[str]:
    """A representative slice of queries Claude actually fires at Hyphae."""
    return [
        "openkeel project status recent work",
        "token saver v4 jagg local llm savings",
        "kanban board task progress",
        "amyloidosis treatment ATTR cardiac",
        "fractal swarm budget modes",
        "kaloth jagg infrastructure ip",
        "calcifer build session 13K lines",
        "llmos launch sprint april",
        "pilgrim cartographer observer stack",
        "hyphae recall scope multi project",
    ]


def bench_recall_rerank() -> dict:
    """Hit the live Hyphae instance with probe queries, then rerank each
    response with qwen2.5:3b on jagg. Measures real round-trip cost and
    real char savings — no synthesis."""
    import urllib.request
    import urllib.error

    out = {
        "queries_run": 0,
        "queries_skipped": 0,
        "results_in": 0,
        "results_kept": 0,
        "original_chars": 0,
        "kept_chars": 0,
        "saved_chars": 0,
        "fell_back": 0,
        "total_latency_ms": 0.0,
        "per_query": [],
        "error": None,
    }

    for q in _probe_recall_queries():
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8100/recall",
                data=json.dumps({"query": q, "top_k": 10}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode())
        except Exception as e:
            out["queries_skipped"] += 1
            out["error"] = f"{type(e).__name__}: {e}"
            continue

        results = payload.get("results") or payload.get("hits") or []
        if not results:
            out["queries_skipped"] += 1
            continue

        decision = recall_rerank.rerank(q, results)

        out["queries_run"] += 1
        out["results_in"] += len(results)
        out["results_kept"] += len(decision.kept_indices)
        out["original_chars"] += decision.original_chars
        out["kept_chars"] += decision.kept_chars
        out["saved_chars"] += decision.saved_chars
        out["total_latency_ms"] += decision.latency_ms
        if decision.fell_back:
            out["fell_back"] += 1
        out["per_query"].append({
            "q": q[:50],
            "n_in": len(results),
            "n_kept": len(decision.kept_indices),
            "orig": decision.original_chars,
            "saved": decision.saved_chars,
            "ratio": round(decision.ratio * 100, 1),
            "ms": round(decision.latency_ms),
            "fb": decision.reason if decision.fell_back else "",
        })

    if out["queries_run"]:
        out["avg_latency_ms"] = round(out["total_latency_ms"] / out["queries_run"], 1)
        out["overall_ratio_pct"] = round(out["saved_chars"] / max(1, out["original_chars"]) * 100, 1)
    return out


def _summarize_engine(name: str, results: list) -> dict:
    """Aggregate per-call results into a summary block."""
    n = len(results)
    if n == 0:
        return {"engine": name, "calls": 0}
    orig = sum(r.original_chars for r in results)
    saved = sum(r.saved_chars for r in results)
    fb = sum(1 for r in results if r.fell_back)
    lat = sum(r.latency_ms for r in results) / n
    return {
        "engine": name,
        "calls": n,
        "original_chars": orig,
        "saved_chars": saved,
        "ratio_pct": round(saved / max(1, orig) * 100, 1),
        "avg_latency_ms": round(lat, 0),
        "fell_back": fb,
    }


def bench_diff_compressor() -> dict:
    """Run on real git history from this repo."""
    import subprocess
    cmds = [
        ["git", "log", "-p", "-n", "3", "--", "openkeel/token_saver_v4/"],
        ["git", "log", "-p", "-n", "5", "--", "openkeel/token_saver/"],
        ["git", "show", "HEAD"],
        ["git", "show", "HEAD~1"],
        ["git", "show", "HEAD~2"],
    ]
    results = []
    for cmd in cmds:
        try:
            blob = subprocess.check_output(cmd, stderr=subprocess.DEVNULL,
                                            cwd="/home/om/openkeel",
                                            timeout=10).decode("utf-8", "replace")
        except Exception:
            continue
        if len(blob) < diff_compressor.MIN_CHARS:
            continue
        results.append(diff_compressor.compress(blob))
    return _summarize_engine("diff_compressor", results)


_SYNTHETIC_TRACES = [
    """Traceback (most recent call last):
  File "/usr/lib/python3.11/site-packages/django/core/handlers/exception.py", line 55, in inner
    response = get_response(request)
  File "/usr/lib/python3.11/site-packages/django/core/handlers/base.py", line 197, in _get_response
    response = wrapped_callback(request, *callback_args, **callback_kwargs)
  File "/usr/lib/python3.11/site-packages/django/views/decorators/csrf.py", line 56, in wrapped_view
    return view_func(*args, **kwargs)
  File "/home/om/myapp/views.py", line 47, in checkout
    total = compute_total(cart_items)
  File "/home/om/myapp/billing.py", line 88, in compute_total
    return sum(item.price * item.qty for item in items) + tax_for(items)
  File "/home/om/myapp/billing.py", line 102, in tax_for
    rate = TAX_RATES[region.code]
KeyError: 'XX'
""" * 4,
    """============================= test session starts ==============================
platform linux -- Python 3.11.6, pytest-8.0.0, pluggy-1.4.0
rootdir: /home/om/myproj
plugins: cov-4.1.0, asyncio-0.23.5, mock-3.12.0
collected 247 items

tests/test_a.py ........................                                [ 10%]
tests/test_b.py ........................                                [ 20%]
tests/test_c.py ............F                                           [ 25%]
=================================== FAILURES ===================================
______________________________ test_compute_tax ________________________________

    def test_compute_tax():
        cart = make_cart([("apple", 2, 1.50), ("bread", 1, 3.00)])
>       assert compute_tax(cart, region="ON") == pytest.approx(0.78)
E       AssertionError: assert 0.6 == 0.78 ± 7.8e-7
E         comparison failed
E         Obtained: 0.6
E         Expected: 0.78 ± 7.8e-7

tests/test_c.py:42: AssertionError
""" * 3,
    """node:internal/process/promises:288
            triggerUncaughtException(err, true /* fromPromise */);
            ^

Error: Cannot find module 'lodash/get'
Require stack:
- /home/om/app/dist/services/normalizer.js
- /home/om/app/dist/services/index.js
- /home/om/app/dist/server.js
    at Module._resolveFilename (node:internal/modules/cjs/loader:1135:15)
    at Module._load (node:internal/modules/cjs/loader:976:27)
    at Module.require (node:internal/modules/cjs/loader:1225:19)
    at require (node:internal/modules/cjs/helpers:177:18)
""" * 5,
]


def bench_error_distiller() -> dict:
    results = []
    for trace in _SYNTHETIC_TRACES:
        results.append(error_distiller.distill(trace))
    return _summarize_engine("error_distiller", results)


_SYNTHETIC_PAGES = [
    ("How do I configure ollama for remote access?",
     ("<html><body><nav>Docs Home</nav><h1>Ollama Configuration</h1>"
      "<p>Lorem ipsum boilerplate intro that nobody reads " * 30 +
      "</p><h2>Remote Access</h2><p>Set OLLAMA_HOST=0.0.0.0:11434 in "
      "your environment. On systemd: edit /etc/systemd/system/ollama."
      "service.d/override.conf and add Environment=OLLAMA_HOST=0.0.0.0"
      ":11434, then systemctl daemon-reload && systemctl restart ollama"
      ".</p>" + "<p>Footer disclaimer copyright stuff " * 40 +
      "</p></body></html>")),
    ("What does the Anthropic prompt caching API charge for cache hits?",
     ("<html><body>" + "<p>Marketing fluff intro " * 50 +
      "<h2>Pricing</h2><table><tr><td>Cache write</td><td>1.25x base "
      "input price</td></tr><tr><td>Cache read</td><td>0.1x base input "
      "price</td></tr></table>" + "<p>FAQ section bla bla " * 80 +
      "</body></html>")),
    ("How do I implement a SQLite WAL mode upgrade?",
     ("<html><body>" + "<div>Sidebar nav links " * 60 +
      "<h2>WAL Mode</h2><p>Run PRAGMA journal_mode=WAL; once. It is "
      "persistent across connections. Use PRAGMA synchronous=NORMAL "
      "for best perf with WAL.</p>" + "<p>Long tutorial digression " * 100
      + "</body></html>")),
]


def bench_webfetch_summarizer() -> dict:
    results = []
    for q, page in _SYNTHETIC_PAGES:
        results.append(webfetch_summarizer.summarize(page, question=q))
    return _summarize_engine("webfetch_summarizer", results)


def bench_pre_compactor() -> dict:
    """Synthesize a realistic transcript from the ledger and compact it."""
    if not LEDGER.exists():
        return {"engine": "pre_compactor", "calls": 0, "error": "no ledger"}
    conn = sqlite3.connect(str(LEDGER))
    rows = conn.execute(
        "SELECT event_type, file_path, original_chars, notes "
        "FROM savings WHERE original_chars > 100 "
        "ORDER BY timestamp DESC LIMIT 80"
    ).fetchall()
    conn.close()

    evt_to_kind = {
        "file_read": "file_read",
        "file_edit": "file_edit",
        "file_write": "file_write",
        "bash_output": "bash_output",
        "grep_output": "grep_output",
    }
    entries = []
    for evt, fp, chars, notes in rows:
        kind = evt_to_kind.get(evt)
        if not kind:
            continue
        body = (notes or "")[:max(1, min(chars, 400))]
        if len(body) < 30:
            body = ("filler content " * (chars // 15))[:chars]
        entries.append({
            "kind": kind,
            "ref_id": fp or "",
            "content": body,
        })

    if len(entries) < 6:
        return {"engine": "pre_compactor", "calls": 0, "error": "no entries"}

    decision = pre_compactor.compact(entries)
    return {
        "engine": "pre_compactor",
        "calls": 1,
        "entries_in": len(entries),
        "entries_pruned": decision.pruned_count,
        "original_chars": decision.original_chars,
        "saved_chars": decision.saved_chars,
        "ratio_pct": round(decision.saved_chars / max(1, decision.original_chars) * 100, 1),
        "avg_latency_ms": round(decision.latency_ms, 0),
        "fell_back": 1 if decision.fell_back else 0,
    }


_GOAL_PROBES = [
    ("openkeel/token_saver/hooks/pre_tool.py",
     "find where Hyphae recall responses are intercepted and rewritten"),
    ("openkeel/token_saver/summarizer.py",
     "understand how the local LLM endpoint is resolved and called"),
    ("openkeel/token_saver_v4/engines/recall_rerank.py",
     "audit the rerank decision logic and fallback paths"),
    ("openkeel/core/cartographer.py",
     "see how the problem map adds nodes and edges"),
    ("openkeel/core/pilgrim.py",
     "find blind-spot detection logic"),
]


def bench_goal_reader() -> dict:
    """Run goal_reader against real source files with realistic goals."""
    import os.path as _osp
    repo = "/home/om/openkeel"
    results = []
    per_file = []
    for rel, goal in _GOAL_PROBES:
        path = _osp.join(repo, rel)
        if not _osp.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            continue
        if len(content) < goal_reader.MIN_CHARS:
            continue
        d = goal_reader.filter_by_goal(content, goal=goal, file_path=rel)
        results.append(d)
        per_file.append({
            "file": rel.rsplit("/", 1)[-1],
            "orig": d.original_chars,
            "out": d.output_chars,
            "saved": d.saved_chars,
            "ratio_pct": round(d.saved_chars / max(1, d.original_chars) * 100, 1),
            "ms": round(d.latency_ms),
            "fb": d.reason if d.fell_back else "",
        })
    if not results:
        return {"engine": "goal_reader", "calls": 0, "error": "no probes loaded"}
    return {
        "engine": "goal_reader",
        "calls": len(results),
        "original_chars": sum(r.original_chars for r in results),
        "saved_chars": sum(r.saved_chars for r in results),
        "ratio_pct": round(
            sum(r.saved_chars for r in results) /
            max(1, sum(r.original_chars for r in results)) * 100, 1),
        "avg_latency_ms": round(
            sum(r.latency_ms for r in results) / max(1, len(results))),
        "fell_back": sum(1 for r in results if r.fell_back),
        "per_file": per_file,
    }


_SYNTHETIC_AGENT_PROMPTS = [
    ("Survey LLMOS codebase state",
     "Please conduct a comprehensive survey of the current state of the "
     "LLMOS codebase. I need you to look at every module under "
     "openkeel/llmos/ and openkeel/core/. For each module, identify: "
     "(1) the public API, (2) what tests exist if any, (3) the level of "
     "completion (stub, partial, complete), (4) any TODO/FIXME comments, "
     "(5) imports it depends on. Be thorough.\n\n"
     "Background context: We are building LLMOS as a long-term project. "
     "It started in early March 2026 with the Calcifer module and has "
     "grown to roughly 13K lines across 50+ files. Recent commits added "
     "the fractal swarm executor and observer stack. The launch sprint "
     "is April 7-11. I need a status report I can use to plan that sprint.\n\n"
     "Specific files of interest if you have time:\n"
     "- openkeel/llmos/__init__.py\n- openkeel/llmos/calcifer.py\n"
     "- openkeel/llmos/fractal/__init__.py\n- openkeel/llmos/observer_daemon.py\n"
     "- openkeel/core/cartographer.py\n- openkeel/core/pilgrim.py\n"
     "- openkeel/core/oracle.py\n- openkeel/core/consensus.py\n\n"
     "Please return findings as a structured report with sections per "
     "module. Include line counts and confidence ratings."),
    ("Audit token saver hooks",
     "Audit the openkeel/token_saver/hooks/ directory for code quality "
     "issues, integration bugs, and missed optimization opportunities. "
     "Focus on pre_tool.py and post_tool.py.\n\n"
     "Background: The token saver intercepts Claude Code tool calls via "
     "PreToolUse and PostToolUse hooks. The pre_tool.py file is around "
     "1300 lines and handles Read/Bash/Grep/Glob/Edit/Write/Agent. It has "
     "many handlers for specific bash command shapes (npm install, pip "
     "install, git push, git diff, test runners, etc). The post_tool.py "
     "is tracking-only and writes to the daemon ledger.\n\n"
     "I want you to specifically look for:\n"
     "1. Race conditions between handlers\n"
     "2. Handlers that double-record savings to the ledger\n"
     "3. Places where regex patterns could miss common cases\n"
     "4. Missing fail-open paths\n"
     "5. Hardcoded paths that should be configurable\n"
     "6. Opportunities to use the local LLM more aggressively\n\n"
     "Return findings as a numbered list, ordered by severity. Include "
     "file:line references for each finding."),
    ("Research token saving competitors",
     "Research how other AI coding assistants handle token efficiency. "
     "I want to understand the state of the art so we can decide if our "
     "token saver approach is competitive or if we are missing major "
     "techniques.\n\n"
     "Background: We have built a token saver for Claude Code that uses "
     "a layered approach - deterministic regex compression first, then "
     "local LLM (qwen2.5:3b on a 3090) for semantic filtering. Current "
     "lifetime savings are around 45 percent.\n\n"
     "Please look into:\n"
     "1. Cursor's approach to context management\n"
     "2. Aider's repo map strategy\n"
     "3. Continue.dev's context selection\n"
     "4. Cline (Claude Dev) and what it does with context\n"
     "5. Anthropic's prompt caching API\n"
     "6. Any academic work on LLM context compression\n\n"
     "I am especially interested in techniques where a small local model "
     "could compress input before it reaches the main model. Return a "
     "comparison table."),
]


def _inflate_prompt(base: str) -> str:
    """Real agent prompts are 17-30K because Claude pastes file context.
    Mimic that by appending real source files."""
    extras = []
    for path in (
        "/home/om/openkeel/openkeel/core/cartographer.py",
        "/home/om/openkeel/openkeel/token_saver/hooks/post_tool.py",
    ):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                extras.append(f"\n\n--- PASTED CONTEXT: {path} ---\n" + f.read()[:9000])
        except Exception:
            continue
    return base + "".join(extras)


def bench_subagent_filter() -> dict:
    results = []
    per_prompt = []
    for desc, prompt in _SYNTHETIC_AGENT_PROMPTS:
        inflated = _inflate_prompt(prompt)
        d = subagent_filter.compress_prompt(inflated, description=desc)
        results.append(d)
        per_prompt.append({
            "desc": desc[:40],
            "orig": d.original_chars,
            "out": d.output_chars,
            "saved": d.saved_chars,
            "ratio_pct": round(d.saved_chars / max(1, d.original_chars) * 100, 1),
            "ms": round(d.latency_ms),
            "fb": d.reason if d.fell_back else "",
        })
    return {
        "engine": "subagent_filter",
        "calls": len(results),
        "original_chars": sum(r.original_chars for r in results),
        "saved_chars": sum(r.saved_chars for r in results),
        "ratio_pct": round(
            sum(r.saved_chars for r in results) /
            max(1, sum(r.original_chars for r in results)) * 100, 1),
        "avg_latency_ms": round(
            sum(r.latency_ms for r in results) / max(1, len(results))),
        "fell_back": sum(1 for r in results if r.fell_back),
        "per_prompt": per_prompt,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print("=" * 60)
    print("TOKEN SAVER v4 — BENCHMARK")
    print("=" * 60)
    print(f"ledger: {LEDGER}")
    print()

    t0 = time.time()
    lingua = bench_lingua()
    t1 = time.time()

    print(f"[lingua] ran in {t1 - t0:.1f}s")
    print()
    print("LINGUA COMPRESSOR (additional savings on top of v3)")
    print("-" * 60)
    print(f"{'event_type':<20} {'samples':>8} {'orig_chars':>12} "
          f"{'v4_add':>10} {'ratio':>7}")
    for evt, s in lingua["by_event"].items():
        ratio = 0.0
        if s["original"]:
            ratio = s["v4_additional"] / s["original"] * 100
        print(f"{evt:<20} {s['samples_tested']:>8} "
              f"{s['original']:>12,} {s['v4_additional']:>10,} "
              f"{ratio:>6.1f}%")
    print("-" * 60)
    total_ratio = 0.0
    if lingua["original_chars"]:
        total_ratio = lingua["v4_additional_chars"] / lingua["original_chars"] * 100
    print(f"{'TOTAL':<20} {lingua['events']:>8} "
          f"{lingua['original_chars']:>12,} "
          f"{lingua['v4_additional_chars']:>10,} "
          f"{total_ratio:>6.1f}%")
    print()

    rerank_results = bench_recall_rerank()
    print("RECALL RERANK (v4.1 — live local LLM filter on Hyphae output)")
    print("-" * 60)
    if rerank_results.get("error") and rerank_results["queries_run"] == 0:
        print(f"  ERROR: {rerank_results['error']}")
    else:
        print(f"  queries_run            {rerank_results['queries_run']}")
        print(f"  queries_skipped        {rerank_results['queries_skipped']}")
        print(f"  results_in / kept      {rerank_results['results_in']} / {rerank_results['results_kept']}")
        print(f"  original_chars         {rerank_results['original_chars']:,}")
        print(f"  saved_chars            {rerank_results['saved_chars']:,}")
        print(f"  overall_ratio          {rerank_results.get('overall_ratio_pct', 0)}%")
        print(f"  avg_latency_ms         {rerank_results.get('avg_latency_ms', 0)}")
        print(f"  fell_back              {rerank_results['fell_back']} / {rerank_results['queries_run']}")
        print()
        print("  per-query:")
        for pq in rerank_results["per_query"]:
            print(f"    n={pq['n_in']:>2}->{pq['n_kept']:<2} "
                  f"orig={pq['orig']:>6} saved={pq['saved']:>6} "
                  f"({pq['ratio']:>5.1f}%) {pq['ms']:>4}ms  {pq['q']}"
                  + (f"  [{pq['fb']}]" if pq['fb'] else ""))
    print()

    print("=" * 60)
    print("v4.2 NEW ENGINES — local LLM as input filter")
    print("=" * 60)
    v42_total_orig = 0
    v42_total_saved = 0
    for fn in (bench_diff_compressor, bench_error_distiller,
               bench_webfetch_summarizer, bench_pre_compactor,
               bench_goal_reader, bench_subagent_filter):
        r = fn()
        name = r.get("engine", "?")
        print(f"\n[{name}]")
        for k, v in r.items():
            if k == "engine":
                continue
            print(f"  {k:<22} {v}")
        v42_total_orig += r.get("original_chars", 0) or 0
        v42_total_saved += r.get("saved_chars", 0) or 0
    print()
    if v42_total_orig:
        print(f"v4.2 ENGINE TOTAL: {v42_total_saved:,} / {v42_total_orig:,} chars "
              f"({v42_total_saved / v42_total_orig * 100:.1f}%)")
        print(f"  ≈ {v42_total_saved // 4:,} Claude tokens saved on this bench run")
    print()

    offload = bench_subagent_offload()
    print("SUBAGENT OFFLOAD (nudge opportunities on historical sessions)")
    print("-" * 60)
    for k, v in offload.items():
        print(f"  {k:<28} {v}")
    print()

    # Combined projection
    v3_total = lingua["v3_saved_chars"]
    v4_add = lingua["v4_additional_chars"]
    orig = lingua["original_chars"]
    if orig:
        print(f"PROJECTED combined savings on replayed events:")
        print(f"  v3 alone:  {v3_total:>10,} / {orig:,}  "
              f"({v3_total / orig * 100:.1f}%)")
        print(f"  v3 + v4:   {v3_total + v4_add:>10,} / {orig:,}  "
              f"({(v3_total + v4_add) / orig * 100:.1f}%)")
        print(f"  v4 delta:  +{v4_add / orig * 100:.1f} percentage points")
    return 0


if __name__ == "__main__":
    sys.exit(main())
