#!/usr/bin/env python3
"""Unified local model endpoint resolver for all Claude-facing LLM paths.

Single source of truth for:
- token saver summarizer
- proxy classifier / MMR
- any Claude-facing local routing

Ensures consistent use of intended GPU (remote 3090 or local Ollama)
and consistent model selection across all subsystems.

Usage:
    from openkeel.token_saver.local_model_resolver import get_endpoint
    url, model = get_endpoint(tier="summarization")
"""

import os
from enum import Enum
from pathlib import Path


class LocalModelTier(Enum):
    """Model tiers by capability requirement."""
    SUMMARIZATION = "summarization"      # bash/grep/file summaries (need 3b-8b)
    TURN_CLASSIFICATION = "turn_class"   # route opus/sonnet/haiku decisions (need 1b-3b)
    MMR_INTERMEDIATE = "mmr"             # replace intermediate turns (need 8b+, high risk)
    CALCIFER_RUNG = "calcifer"           # calcifer ladder rung responses (need 3b-8b)


class GPUTarget(Enum):
    """Where to run the model."""
    REMOTE_3090 = "remote_3090"          # 192.168.0.224:11434 (jagg)
    LOCAL_3070 = "local_3070"            # 127.0.0.1:11434 (kaloth)
    AUTO = "auto"                        # auto-detect best available


# Configuration: tier → (preferred GPU, fallback GPU, recommended models, max_tokens)
TIER_CONFIG = {
    LocalModelTier.SUMMARIZATION: {
        "preferred": GPUTarget.REMOTE_3090,
        "fallback": GPUTarget.LOCAL_3070,
        "models": ["qwen2.5:3b", "gemma4:e2b", "qwen3:8b"],
        "max_tokens": 800,
        "timeout_sec": 30,
        "description": "Summarize bash/grep/file outputs",
    },
    LocalModelTier.TURN_CLASSIFICATION: {
        "preferred": GPUTarget.REMOTE_3090,
        "fallback": GPUTarget.LOCAL_3070,
        "models": ["qwen2.5:3b", "gemma3:1b", "gemma4:e2b"],
        "max_tokens": 4,
        "timeout_sec": 2,
        "description": "Classify turn difficulty (opus/sonnet/haiku)",
    },
    LocalModelTier.MMR_INTERMEDIATE: {
        "preferred": GPUTarget.REMOTE_3090,
        "fallback": None,  # no fallback for high-risk path
        "models": ["qwen3:8b", "qwen2.5:3b"],
        "max_tokens": 1000,
        "timeout_sec": 10,
        "description": "Replace intermediate turns (HIGH RISK)",
    },
    LocalModelTier.CALCIFER_RUNG: {
        "preferred": GPUTarget.REMOTE_3090,
        "fallback": GPUTarget.LOCAL_3070,
        "models": ["qwen2.5:3b", "gemma4:e2b"],
        "max_tokens": 600,
        "timeout_sec": 15,
        "description": "Calcifer ladder rung answers",
    },
}

# GPU target → (endpoint_url_env, model_env, endpoint_default, model_defaults_by_tier)
GPU_ENDPOINTS = {
    GPUTarget.REMOTE_3090: {
        "endpoint_env": "TOKEN_SAVER_REMOTE_OLLAMA_URL",
        "model_env": "TOKEN_SAVER_REMOTE_OLLAMA_MODEL",
        "endpoint_default": "http://192.168.0.224:11434",
        "model_default": "qwen2.5:3b",
        "description": "Remote 3090 (jagg)",
    },
    GPUTarget.LOCAL_3070: {
        "endpoint_env": "TOKEN_SAVER_LOCAL_OLLAMA_URL",
        "model_env": "TOKEN_SAVER_LOCAL_OLLAMA_MODEL",
        "endpoint_default": "http://127.0.0.1:11434",
        "model_default": "gemma4:e2b",
        "description": "Local 3070 (kaloth)",
    },
}


def get_endpoint(tier: LocalModelTier | str = "summarization", target: GPUTarget | str = "auto") -> tuple[str, str]:
    """Get the (url, model) pair for a Claude-facing LLM task.

    Args:
        tier: LocalModelTier enum or string ("summarization", "turn_class", "mmr", "calcifer")
        target: GPUTarget enum or string ("remote_3090", "local_3070", "auto")

    Returns:
        (endpoint_url, model_name) tuple ready for ollama_generate() calls

    Raises:
        ValueError: if tier is invalid or no suitable endpoint found
    """
    # Normalize inputs
    if isinstance(tier, str):
        tier = LocalModelTier(tier)
    if isinstance(target, str):
        target = GPUTarget(target)

    if tier not in TIER_CONFIG:
        raise ValueError(f"Unknown tier: {tier}")

    config = TIER_CONFIG[tier]
    preferred_target = config["preferred"]
    fallback_target = config.get("fallback")

    # Resolve target: if "auto", use preferred unless it's unavailable
    if target == GPUTarget.AUTO:
        target = preferred_target
        # TODO: add runtime availability check here
        #  if not _endpoint_available(target):
        #      if fallback_target:
        #          target = fallback_target

    # Resolve endpoint and model
    endpoint_cfg = GPU_ENDPOINTS.get(target)
    if not endpoint_cfg:
        raise ValueError(f"Unknown target: {target}")

    endpoint = os.environ.get(
        endpoint_cfg["endpoint_env"],
        endpoint_cfg["endpoint_default"],
    )
    model = os.environ.get(
        endpoint_cfg["model_env"],
        endpoint_cfg["model_default"],
    )

    return endpoint, model


def get_config(tier: LocalModelTier | str) -> dict:
    """Get full configuration for a tier (timeout, max_tokens, etc)."""
    if isinstance(tier, str):
        tier = LocalModelTier(tier)
    return TIER_CONFIG[tier]


def list_endpoints() -> dict:
    """Return all configured endpoints and their current state."""
    endpoints = {}
    for gpu_target, cfg in GPU_ENDPOINTS.items():
        endpoint = os.environ.get(cfg["endpoint_env"], cfg["endpoint_default"])
        model = os.environ.get(cfg["model_env"], cfg["model_default"])
        endpoints[gpu_target.value] = {
            "endpoint": endpoint,
            "model": model,
            "description": cfg["description"],
        }
    return endpoints


def set_preferred_gpu(tier: LocalModelTier | str, target: GPUTarget | str):
    """Override preferred GPU for a tier (for testing/debugging)."""
    if isinstance(tier, str):
        tier = LocalModelTier(tier)
    if isinstance(target, str):
        target = GPUTarget(target)

    if tier not in TIER_CONFIG:
        raise ValueError(f"Unknown tier: {tier}")
    if target not in GPU_ENDPOINTS:
        raise ValueError(f"Unknown target: {target}")

    TIER_CONFIG[tier]["preferred"] = target


# Backward-compatibility shims
def get_fast_endpoint() -> tuple[str, str, str] | None:
    """Legacy: return (url, model, tier) for fast LLM path."""
    url, model = get_endpoint(LocalModelTier.SUMMARIZATION)
    return url, model, "summarization"


if __name__ == "__main__":
    import json

    print("=== Local Model Endpoint Configuration ===\n")
    print("All Endpoints:")
    print(json.dumps(list_endpoints(), indent=2, default=str))

    print("\n\nTier Configurations:")
    for tier, cfg in TIER_CONFIG.items():
        print(f"\n{tier.value}:")
        print(f"  Description: {cfg['description']}")
        print(f"  Preferred: {cfg['preferred'].value}")
        print(f"  Fallback: {cfg['fallback'].value if cfg['fallback'] else 'none'}")
        print(f"  Models: {', '.join(cfg['models'])}")
        print(f"  Max tokens: {cfg['max_tokens']}")
        print(f"  Timeout: {cfg['timeout_sec']}s")
        url, model = get_endpoint(tier)
        print(f"  → Current: {url} / {model}")
