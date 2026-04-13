"""Bubble engine — token-saving gather-then-reason pattern."""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .gather import gather
from .reason import reason_sonnet_cli, reason_local, reason_ultra, reason_cascade, run_vanilla
from .router import should_use_bubble
from openkeel.hyphae import client as hyphae
from . import ollama as local_llm
from . import settings

LOG_DIR = Path.home() / ".openkeel2" / "logs"


def run(task, repo_path, verbose=True, project=None, local_mode=None):
    """Run the bubble v4 pattern. Returns (output, api_cost, details)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    t0 = time.time()

    # v4: Resolve local model settings
    cfg = settings.load()
    if local_mode is None:
        local_mode = cfg["local_for"]
    local_model = cfg["local_model"] if local_mode != "off" else None

    use_local_gather = local_model and local_mode in ("gather", "both", "ultra", "cascade")
    use_local_reason = local_model and local_mode in ("reason", "both")
    use_ultra = local_model and local_mode == "ultra"
    use_cascade = local_model and local_mode == "cascade"

    if verbose and local_model and local_mode != "off":
        loaded = [m.get("name", "") for m in local_llm.get_loaded()]
        warm_status = "warm" if local_model in loaded else "cold"
        print(f"[bubble] Local LLM: {local_model} ({warm_status})", file=sys.stderr)
        parts = []
        if use_cascade:
            parts.append("cascade (Haiku classifies → local or Sonnet)")
        elif use_ultra:
            parts.append("ultra (local gather + local reason + Haiku judge)")
        else:
            if use_local_gather:
                parts.append("gather")
            if use_local_reason:
                parts.append("reason")
        print(f"[bubble] Local mode: {' + '.join(parts)}", file=sys.stderr)

    # v3: Query Hyphae for relevant context
    hyphae_context = ""
    hyphae_available = False
    if hyphae.is_available():
        hyphae_available = True
        if verbose:
            print("[bubble] Hyphae connected — recalling relevant context...", file=sys.stderr)
        hyphae_context = hyphae.get_context_for_task(task, project=project)
        if hyphae_context and verbose:
            print(f"[bubble] Got {len(hyphae_context)} chars of memory context", file=sys.stderr)
    elif verbose:
        print("[bubble] Hyphae not available — proceeding without memory", file=sys.stderr)

    # Task routing
    use_bubble = should_use_bubble(task)

    if not use_bubble:
        if verbose:
            print("[bubble] Task routed to VANILLA (broad/simple task)", file=sys.stderr)
        output, wall_ms = run_vanilla(task, repo_path)
        log = {
            "run_id": run_id, "version": "v4",
            "task": task[:500], "repo": repo_path,
            "routed": "vanilla", "api_cost": 0,
            "wall_ms": wall_ms, "output_len": len(output or ""),
            "hyphae": hyphae_available,
            "local_model": local_model,
            "local_mode": local_mode,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _save_log(run_id, log)
        if hyphae_available and output:
            hyphae.remember(f"Bubble analyzed: {task[:200]} — routed to vanilla, {len(output)} chars output")
        if verbose:
            print(f"[bubble] Vanilla done in {wall_ms}ms", file=sys.stderr)
        return output, 0, log

    # Gather phase
    if use_local_gather:
        if verbose:
            print(f"[bubble] Gathering via local LLM ({local_model})...", file=sys.stderr)
        gathered, gather_cost, gather_details = gather(
            task, repo_path, hyphae_context=hyphae_context, local_model=local_model
        )
    else:
        if verbose:
            print("[bubble] Gathering via Haiku API...", file=sys.stderr)
        gathered, gather_cost, gather_details = gather(
            task, repo_path, hyphae_context=hyphae_context
        )

    if verbose:
        quality = gather_details["gather_quality"]
        hflag = " +hyphae" if gather_details.get("had_hyphae") else ""
        lflag = f" +local:{local_model}" if gather_details.get("local_gather") else ""
        print(
            f"[bubble] Gathered {len(gathered)} chars, ${gather_cost:.4f} (quality: {quality}{hflag}{lflag})",
            file=sys.stderr,
        )

    # Quality gate
    if gather_details["gather_quality"] == "poor":
        if verbose:
            print("[bubble] Gather quality poor — falling back to VANILLA", file=sys.stderr)
        output, reason_ms = run_vanilla(task, repo_path)
        wall_ms = round((time.time() - t0) * 1000)
        log = {
            "run_id": run_id, "version": "v4",
            "task": task[:500], "repo": repo_path,
            "routed": "vanilla_fallback", "api_cost": round(gather_cost, 6),
            "wall_ms": wall_ms, "gather": gather_details,
            "reason_ms": reason_ms, "output_len": len(output or ""),
            "hyphae": hyphae_available,
            "local_model": local_model,
            "local_mode": local_mode,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _save_log(run_id, log)
        if verbose:
            print(f"[bubble] Vanilla fallback done in {wall_ms}ms", file=sys.stderr)
        return output, gather_cost, log

    # Reason phase
    escalated = False
    cascade_info = None
    if use_cascade:
        if verbose:
            print(f"[bubble] Cascade mode: Haiku classifying difficulty...", file=sys.stderr)
        output, reason_ms, cascade_info = reason_cascade(
            task, gathered, local_model, repo_path, hyphae_context=hyphae_context
        )
        escalated = cascade_info.get("escalated", False)
        if verbose:
            tier = cascade_info["tier"]
            if tier == "local":
                print(f"[bubble] Cascade → local only (easy task, zero reason cost)", file=sys.stderr)
            elif tier == "local+judge":
                esc = " → escalated to Sonnet" if escalated else " → passed quality gate"
                print(f"[bubble] Cascade → local + Haiku judge{esc}", file=sys.stderr)
            else:
                print(f"[bubble] Cascade → Sonnet direct (hard task)", file=sys.stderr)
    elif use_ultra:
        if verbose:
            print(f"[bubble] Ultra mode: local reason ({local_model}) + Haiku quality gate...", file=sys.stderr)
        output, reason_ms, escalated = reason_ultra(
            task, gathered, local_model, repo_path, hyphae_context=hyphae_context
        )
        if verbose and escalated:
            print("[bubble] Haiku escalated to Sonnet (local quality too low)", file=sys.stderr)
        elif verbose:
            print("[bubble] Local output passed Haiku quality gate", file=sys.stderr)
    elif use_local_reason:
        if verbose:
            print(f"[bubble] Reasoning via local LLM ({local_model})...", file=sys.stderr)
        output, reason_ms = reason_local(
            task, gathered, local_model, repo_path, hyphae_context=hyphae_context
        )
    else:
        if verbose:
            print("[bubble] Reasoning via Sonnet CLI...", file=sys.stderr)
        output, reason_ms = reason_sonnet_cli(
            task, gathered, repo_path, hyphae_context=hyphae_context
        )

    wall_ms = round((time.time() - t0) * 1000)

    if verbose:
        print(f"[bubble] Done in {wall_ms}ms", file=sys.stderr)
        if escalated:
            print(f"[bubble] API cost: ${gather_cost:.4f} + Haiku judge + Sonnet escalation", file=sys.stderr)
        elif gather_cost > 0:
            print(f"[bubble] API cost: ${gather_cost:.4f}", file=sys.stderr)
        else:
            print(f"[bubble] API cost: $0 (local)", file=sys.stderr)
        print(f"[bubble] Gather: {gather_details['elapsed_ms']}ms, Reason: {reason_ms}ms", file=sys.stderr)

    log = {
        "run_id": run_id, "version": "v4",
        "task": task[:500], "repo": repo_path,
        "routed": "bubble", "api_cost": round(gather_cost, 6),
        "wall_ms": wall_ms, "gather": gather_details,
        "reason_ms": reason_ms, "output_len": len(output or ""),
        "hyphae": hyphae_available,
        "hyphae_context_len": len(hyphae_context),
        "local_model": local_model,
        "local_mode": local_mode,
        "escalated": escalated,
        "cascade": cascade_info,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _save_log(run_id, log)

    # Remember key findings
    if hyphae_available and output:
        mode_tag = f" [local:{local_mode}]" if local_mode != "off" else ""
        summary = f"Bubble analyzed: {task[:200]} — {gather_details['gathered_len']} chars gathered, {len(output)} chars output{mode_tag}"
        hyphae.remember(summary)

    if verbose:
        print(f"[bubble] Log: {LOG_DIR / f'bubble-{run_id}.json'}", file=sys.stderr)

    return output, gather_cost, log


def _save_log(run_id, log):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"bubble-{run_id}.json"
        with open(log_path, "w") as f:
            json.dump(log, f, indent=2)
    except Exception:
        pass

