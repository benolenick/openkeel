"""GPU Tier Detection — scales token saver capabilities based on available GPU power.

Probes local and remote Ollama instances, detects loaded models and VRAM,
and assigns a tier that determines which token saver features are enabled.

Tiers:
  0 — No GPU / No Ollama     → rule-based compression only
  1 — Small model (≤8B)      → simple LocalEdit, basic summarization, convo compress
  2 — Medium model (12-27B)  → complex LocalEdit, multi-line edits, smart summaries
  3 — Large model (>30B)     → full code delegation, architectural reasoning

Usage:
    from openkeel.token_saver.engines.gpu_tier import get_tier, get_best_endpoint
    tier = get_tier()
    endpoint = get_best_endpoint()  # Returns (url, model_name, tier)
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

# Known model sizes (approximate param count in billions)
_MODEL_SIZES: dict[str, float] = {
    "gemma3:1b": 1.0,
    "qwen2.5:1.5b": 1.5,
    "qwen2.5:3b": 3.1,
    "gemma4:e2b": 5.1,
    "gemma4:e4b": 8.0,
    "gemma4:26b": 26.0,
    "gemma4:31b": 31.0,
    "qwen3:8b": 8.0,
    "qwen3.5:latest": 8.0,
    "llama3:8b": 8.0,
    "llama3.1:8b": 8.0,
    "mistral:7b": 7.0,
    "codestral:latest": 22.0,
    "deepseek-coder-v2:16b": 16.0,
    "gpt-oss:20b": 20.0,
}

# Models well-suited for the "hot path" (summarize, compress, simple-edit)
# — small enough to serve at >150 tok/s on a 3090. Order = preference.
_FAST_PATH_MODELS = (
    "qwen2.5:3b",
    "qwen2.5:1.5b",
    "gemma3:1b",
    "gemma4:e2b",
)

# Tier thresholds (param billions)
_TIER_THRESHOLDS = [
    (30.0, 3),   # ≥30B → tier 3
    (12.0, 2),   # ≥12B → tier 2
    (1.0, 1),    # ≥1B  → tier 1
]


@dataclass
class OllamaEndpoint:
    url: str
    name: str  # e.g. "local", "jagg"
    models: list[dict[str, Any]]
    gpu_free_mb: int = 0
    reachable: bool = False
    latency_ms: int = 0


@dataclass
class TierInfo:
    tier: int
    tier_name: str
    endpoint_url: str
    endpoint_name: str
    model_name: str
    model_params_b: float
    features: list[str]


# Endpoints to probe (local first, then remote)
_ENDPOINTS = [
    ("http://127.0.0.1:11434", "local"),
    ("http://192.168.0.224:11434", "jagg"),
]

# Cache the tier for 60 seconds
_cache: dict[str, Any] = {"tier": None, "ts": 0}
_CACHE_TTL = 60


def _probe_endpoint(url: str, name: str) -> OllamaEndpoint:
    """Probe an Ollama endpoint for available models and latency."""
    ep = OllamaEndpoint(url=url, name=name, models=[], reachable=False)

    try:
        t0 = time.time()
        req = urllib.request.Request(f"{url}/api/ps", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            ep.latency_ms = int((time.time() - t0) * 1000)
            ep.reachable = True
            ep.models = data.get("models", [])
    except Exception:
        return ep

    # Also get full model list (not just loaded)
    try:
        req = urllib.request.Request(f"{url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            # Add available-but-not-loaded models
            loaded_names = {m["name"] for m in ep.models}
            for m in data.get("models", []):
                if m["name"] not in loaded_names:
                    ep.models.append({
                        "name": m["name"],
                        "loaded": False,
                        "size": m.get("size", 0),
                        "details": m.get("details", {}),
                    })
                else:
                    # Mark loaded models
                    for loaded in ep.models:
                        if loaded["name"] == m["name"]:
                            loaded["loaded"] = True
    except Exception:
        pass

    return ep


def _estimate_params(model: dict) -> float:
    """Estimate model parameter count in billions."""
    name = model.get("name", "")

    # Check known sizes first
    if name in _MODEL_SIZES:
        return _MODEL_SIZES[name]

    # Try to parse from details
    details = model.get("details", {})
    param_str = details.get("parameter_size", "")
    if param_str:
        # Parse "5.1B", "26B", etc.
        param_str = param_str.upper().replace("B", "").strip()
        try:
            return float(param_str)
        except ValueError:
            pass

    # Estimate from file size (very rough: Q4 ≈ 0.5 bytes/param)
    size_bytes = model.get("size", 0)
    if size_bytes > 0:
        return size_bytes / (0.5 * 1e9)

    return 0.0


def _params_to_tier(params_b: float) -> int:
    """Map parameter count to tier."""
    for threshold, tier in _TIER_THRESHOLDS:
        if params_b >= threshold:
            return tier
    return 1 if params_b > 0 else 0


_TIER_NAMES = {
    0: "rule-only",
    1: "basic",
    2: "advanced",
    3: "full",
}

_TIER_FEATURES = {
    0: ["rule_compression", "bash_compress", "grep_compress", "glob_compress", "prefill"],
    1: ["rule_compression", "bash_compress", "grep_compress", "glob_compress", "prefill",
        "simple_local_edit", "basic_summarize", "conversation_compress"],
    2: ["rule_compression", "bash_compress", "grep_compress", "glob_compress", "prefill",
        "simple_local_edit", "complex_local_edit", "smart_summarize",
        "conversation_compress", "code_review"],
    3: ["rule_compression", "bash_compress", "grep_compress", "glob_compress", "prefill",
        "simple_local_edit", "complex_local_edit", "code_generation",
        "smart_summarize", "conversation_compress", "code_review",
        "architectural_reasoning"],
}


def detect_tier() -> TierInfo:
    """Probe all endpoints and return the best available tier."""
    best_tier = 0
    best_model = ""
    best_params = 0.0
    best_url = ""
    best_name = "none"

    for url, name in _ENDPOINTS:
        ep = _probe_endpoint(url, name)
        if not ep.reachable:
            continue

        for model in ep.models:
            params = _estimate_params(model)
            tier = _params_to_tier(params)

            # Prefer loaded models, then higher tier, then lower latency
            is_loaded = model.get("loaded", True)  # ps results are loaded by default
            score = tier * 1000 + (500 if is_loaded else 0) - ep.latency_ms

            if tier > best_tier or (tier == best_tier and score > best_tier * 1000):
                best_tier = tier
                best_model = model["name"]
                best_params = params
                best_url = url
                best_name = name

    # If nothing found, tier 0
    if not best_model:
        return TierInfo(
            tier=0, tier_name="rule-only",
            endpoint_url="", endpoint_name="none",
            model_name="", model_params_b=0,
            features=_TIER_FEATURES[0],
        )

    return TierInfo(
        tier=best_tier,
        tier_name=_TIER_NAMES.get(best_tier, "unknown"),
        endpoint_url=best_url,
        endpoint_name=best_name,
        model_name=best_model,
        model_params_b=best_params,
        features=_TIER_FEATURES.get(best_tier, _TIER_FEATURES[0]),
    )


def get_tier() -> TierInfo:
    """Get current tier (cached for 60s)."""
    now = time.time()
    if _cache["tier"] and now - _cache["ts"] < _CACHE_TTL:
        return _cache["tier"]

    tier = detect_tier()
    _cache["tier"] = tier
    _cache["ts"] = now
    return tier


def get_best_endpoint() -> tuple[str, str, int]:
    """Returns (url, model_name, tier) for the biggest available model.

    Use for COMPLEX work. For hot-path (compress/summarize/simple-edit) use
    get_fast_endpoint() — small models on a 3090 smoke the 26B and beat Claude.
    """
    t = get_tier()
    return t.endpoint_url, t.model_name, t.tier


def get_fast_endpoint() -> tuple[str, str, int] | None:
    """Return (url, model, tier) for the fastest small model across all endpoints.

    Picks from _FAST_PATH_MODELS in preference order. Prefers endpoints where
    the model is already LOADED. Returns None if no fast model is available
    anywhere — callers should fall back to get_best_endpoint().
    """
    best: tuple[str, str, int, int] | None = None
    for url, name in _ENDPOINTS:
        ep = _probe_endpoint(url, name)
        if not ep.reachable:
            continue
        for model in ep.models:
            mname = model.get("name", "")
            if mname not in _FAST_PATH_MODELS:
                continue
            pref_idx = _FAST_PATH_MODELS.index(mname)
            is_loaded = model.get("loaded", False)
            score = -(pref_idx * 1000) + (500 if is_loaded else 0) - ep.latency_ms
            if best is None or score > best[3]:
                best = (url, mname, 1, score)
    if best is None:
        return None
    return (best[0], best[1], best[2])


def can_do(feature: str) -> bool:
    """Check if a feature is available at the current tier."""
    t = get_tier()
    return feature in t.features


def status_line() -> str:
    """One-line status for dashboard/logging."""
    t = get_tier()
    if t.tier == 0:
        return "Tier 0 (rule-only) — no GPU/model detected"
    return (
        f"Tier {t.tier} ({t.tier_name}) — {t.model_name} "
        f"({t.model_params_b:.0f}B) on {t.endpoint_name} [{t.endpoint_url}]"
    )


if __name__ == "__main__":
    info = detect_tier()
    print(f"Tier: {info.tier} ({info.tier_name})")
    print(f"Model: {info.model_name} ({info.model_params_b:.1f}B)")
    print(f"Endpoint: {info.endpoint_name} ({info.endpoint_url})")
    print(f"Features: {', '.join(info.features)}")
