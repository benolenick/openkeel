"""Local LLM integration via Ollama for bubble v4."""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

OLLAMA_ENDPOINT = os.environ.get("BUBBLE_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_TIMEOUT = 180  # local models can be slow on first load
USAGE_FILE = Path.home() / ".openkeel2" / "usage.json"


def _post(path, data, timeout=None):
    """POST JSON to Ollama. Returns parsed response or None."""
    try:
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_ENDPOINT}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout or OLLAMA_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _get(path, timeout=5):
    """GET from Ollama."""
    try:
        req = urllib.request.Request(f"{OLLAMA_ENDPOINT}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def is_available():
    """Check if Ollama is running."""
    return _get("/api/tags") is not None


def list_models():
    """List available Ollama models. Returns list of model name strings."""
    data = _get("/api/tags")
    if data:
        return [m["name"] for m in data.get("models", [])]
    return []


def get_loaded():
    """Get currently loaded/warm models. Returns list of dicts."""
    data = _get("/api/ps")
    if data:
        return data.get("models", [])
    return []


def keep_warm(model, duration="24h", verbose=False):
    """Pin a model in VRAM by loading it with a long keep_alive.

    Returns True if successful.
    """
    if verbose:
        print(f"[bubble] Warming {model} (keep_alive={duration})...", file=sys.stderr)
    data = _post("/api/generate", {
        "model": model,
        "prompt": "",
        "keep_alive": duration,
    }, timeout=300)
    ok = data is not None
    if verbose:
        if ok:
            print(f"[bubble] {model} is warm and pinned", file=sys.stderr)
        else:
            print(f"[bubble] Failed to warm {model}", file=sys.stderr)
    return ok


def generate(prompt, model, system=None, max_tokens=1024):
    """Generate text with a local model. Returns (response_text, elapsed_ms) or ("", 0).

    Also tracks usage stats to ~/.openkeel2/usage.json.
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "24h",
        "options": {
            "num_predict": max_tokens,
            "temperature": 0.3,
        },
    }
    if system:
        payload["system"] = system

    data = _post("/api/generate", payload)
    if data:
        elapsed = data.get("total_duration", 0) // 1_000_000  # ns -> ms

        # Track usage stats from Ollama's response
        _record_usage(model, data)

        return data.get("response", ""), elapsed
    return "", 0


def _record_usage(model, response_data):
    """Accumulate usage stats from an Ollama response."""
    try:
        stats = load_usage()

        # Per-model stats
        if model not in stats["models"]:
            stats["models"][model] = {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_duration_ms": 0,
                "prompt_eval_ms": 0,
                "eval_ms": 0,
            }

        m = stats["models"][model]
        m["calls"] += 1
        m["prompt_tokens"] += response_data.get("prompt_eval_count", 0)
        m["completion_tokens"] += response_data.get("eval_count", 0)
        m["total_duration_ms"] += response_data.get("total_duration", 0) // 1_000_000
        m["prompt_eval_ms"] += response_data.get("prompt_eval_duration", 0) // 1_000_000
        m["eval_ms"] += response_data.get("eval_duration", 0) // 1_000_000

        # Global totals
        stats["total_calls"] += 1
        stats["total_prompt_tokens"] += response_data.get("prompt_eval_count", 0)
        stats["total_completion_tokens"] += response_data.get("eval_count", 0)
        stats["total_duration_ms"] += response_data.get("total_duration", 0) // 1_000_000
        stats["last_call"] = time.strftime("%Y-%m-%d %H:%M:%S")

        # Last call speed (tokens/sec)
        eval_dur = response_data.get("eval_duration", 0)
        eval_count = response_data.get("eval_count", 0)
        if eval_dur > 0 and eval_count > 0:
            stats["last_tok_per_sec"] = round(eval_count / (eval_dur / 1e9), 1)

        save_usage(stats)
    except Exception:
        pass


def load_usage():
    """Load accumulated usage stats."""
    default = {
        "models": {},
        "total_calls": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_duration_ms": 0,
        "last_call": None,
        "last_tok_per_sec": 0,
    }
    if USAGE_FILE.exists():
        try:
            data = json.loads(USAGE_FILE.read_text())
            # Merge with defaults for any missing keys
            for k, v in default.items():
                if k not in data:
                    data[k] = v
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return default


def save_usage(stats):
    """Save usage stats to disk."""
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps(stats, indent=2) + "\n")


def reset_usage():
    """Reset usage stats."""
    if USAGE_FILE.exists():
        USAGE_FILE.unlink()


def format_usage():
    """Format usage stats for display. Returns a multi-line string."""
    stats = load_usage()
    lines = []
    lines.append("Local LLM Usage Stats")
    lines.append("=" * 45)

    if stats["total_calls"] == 0:
        lines.append("  No local LLM calls recorded yet.")
        return "\n".join(lines)

    total_tok = stats["total_prompt_tokens"] + stats["total_completion_tokens"]
    total_sec = stats["total_duration_ms"] / 1000

    lines.append(f"  Total calls:        {stats['total_calls']}")
    lines.append(f"  Prompt tokens:      {stats['total_prompt_tokens']:,}")
    lines.append(f"  Completion tokens:  {stats['total_completion_tokens']:,}")
    lines.append(f"  Total tokens:       {total_tok:,}")
    lines.append(f"  Total GPU time:     {total_sec:.1f}s")

    if total_tok > 0 and total_sec > 0:
        avg_tps = total_tok / total_sec
        lines.append(f"  Avg throughput:     {avg_tps:.1f} tok/s")

    if stats["last_tok_per_sec"]:
        lines.append(f"  Last gen speed:     {stats['last_tok_per_sec']} tok/s")

    if stats["last_call"]:
        lines.append(f"  Last call:          {stats['last_call']}")

    # Estimated API savings
    # If these tokens went to Haiku: $0.80/M in + $4.00/M out
    # If these tokens went to Sonnet: ~$3/M in + $15/M out
    haiku_cost = (stats["total_prompt_tokens"] * 0.80 + stats["total_completion_tokens"] * 4.00) / 1_000_000
    sonnet_cost = (stats["total_prompt_tokens"] * 3.00 + stats["total_completion_tokens"] * 15.00) / 1_000_000

    lines.append("")
    lines.append("  Estimated API savings (if these ran on API):")
    lines.append(f"    vs Haiku:   ${haiku_cost:.4f}")
    lines.append(f"    vs Sonnet:  ${sonnet_cost:.4f}")

    # Per-model breakdown
    if len(stats["models"]) > 0:
        lines.append("")
        lines.append("  Per-model breakdown:")
        for name, m in stats["models"].items():
            mtok = m["prompt_tokens"] + m["completion_tokens"]
            msec = m["total_duration_ms"] / 1000
            tps = mtok / msec if msec > 0 else 0
            lines.append(f"    {name}: {m['calls']} calls, {mtok:,} tok, {msec:.1f}s, {tps:.1f} tok/s")

    return "\n".join(lines)
