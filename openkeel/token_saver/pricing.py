"""Model pricing database — real $/token costs for accurate ledger reporting.

Inspired by LiteLLM's cost tracking. Prices are per 1M tokens.
Updated manually — check https://openai.com/pricing and
https://www.anthropic.com/pricing for latest.

Last updated: 2026-04-05
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LAST_UPDATED = "2026-04-05"


@dataclass
class ModelPricing:
    name: str
    provider: str
    input_per_1m: float    # $/1M input tokens
    output_per_1m: float   # $/1M output tokens
    cached_input_per_1m: float = 0.0  # $/1M cached input tokens (if supported)
    context_window: int = 0
    notes: str = ""


# ---------------------------------------------------------------------------
# Pricing database
# ---------------------------------------------------------------------------

MODELS: dict[str, ModelPricing] = {
    # Anthropic
    "claude-opus-4": ModelPricing("Claude Opus 4", "Anthropic", 15.0, 75.0, 1.50, 200000),
    "claude-sonnet-4": ModelPricing("Claude Sonnet 4", "Anthropic", 3.0, 15.0, 0.30, 200000),
    "claude-haiku-3.5": ModelPricing("Claude Haiku 3.5", "Anthropic", 0.80, 4.0, 0.08, 200000),
    "claude-opus-4-1m": ModelPricing("Claude Opus 4 (1M)", "Anthropic", 15.0, 75.0, 1.50, 1000000, "extended context"),

    # OpenAI
    "gpt-5": ModelPricing("GPT-5", "OpenAI", 10.0, 30.0, 2.50, 128000),
    "gpt-4o": ModelPricing("GPT-4o", "OpenAI", 2.50, 10.0, 1.25, 128000),
    "gpt-4o-mini": ModelPricing("GPT-4o Mini", "OpenAI", 0.15, 0.60, 0.075, 128000),
    "o3": ModelPricing("o3", "OpenAI", 10.0, 40.0, 2.50, 200000),
    "o3-mini": ModelPricing("o3-mini", "OpenAI", 1.10, 4.40, 0.55, 200000),
    "o4-mini": ModelPricing("o4-mini", "OpenAI", 1.10, 4.40, 0.55, 200000),

    # Google
    "gemini-2.5-pro": ModelPricing("Gemini 2.5 Pro", "Google", 1.25, 10.0, 0.315, 1000000),
    "gemini-2.5-flash": ModelPricing("Gemini 2.5 Flash", "Google", 0.15, 0.60, 0.0375, 1000000),
    "gemini-3-pro": ModelPricing("Gemini 3 Pro", "Google", 2.50, 15.0, 0.50, 2000000),

    # DeepSeek
    "deepseek-chat": ModelPricing("DeepSeek V3", "DeepSeek", 0.27, 1.10, 0.07, 128000),
    "deepseek-reasoner": ModelPricing("DeepSeek R1", "DeepSeek", 0.55, 2.19, 0.14, 128000),

    # Local (free)
    "gemma4:e2b": ModelPricing("Gemma 4 E2B", "Local/Ollama", 0.0, 0.0, 0.0, 32000, "local GPU"),
    "qwen3:8b": ModelPricing("Qwen 3 8B", "Local/Ollama", 0.0, 0.0, 0.0, 32000, "local GPU"),
    "qwen3.5:latest": ModelPricing("Qwen 3.5", "Local/Ollama", 0.0, 0.0, 0.0, 32000, "local GPU"),
    "llama3:8b": ModelPricing("Llama 3 8B", "Local/Ollama", 0.0, 0.0, 0.0, 8000, "local GPU"),
}

# Quick lookup aliases
ALIASES: dict[str, str] = {
    "opus": "claude-opus-4",
    "opus-4": "claude-opus-4",
    "claude-opus-4-6": "claude-opus-4",
    "sonnet": "claude-sonnet-4",
    "sonnet-4": "claude-sonnet-4",
    "claude-sonnet-4-6": "claude-sonnet-4",
    "haiku": "claude-haiku-3.5",
    "gpt4o": "gpt-4o",
    "gpt-4o-2024": "gpt-4o",
    "deepseek": "deepseek-chat",
}


def get_pricing(model: str) -> ModelPricing | None:
    """Look up pricing for a model by name or alias."""
    model_lower = model.lower().strip()
    key = ALIASES.get(model_lower, model_lower)
    return MODELS.get(key)


def estimate_cost(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    model: str = "claude-opus-4",
) -> dict[str, Any]:
    """Estimate the cost of a request.

    Returns:
        {
            "model": str,
            "input_cost": float,
            "output_cost": float,
            "cached_cost": float,
            "total_cost": float,
            "input_tokens": int,
            "output_tokens": int,
        }
    """
    pricing = get_pricing(model)
    if not pricing:
        # Default to Opus pricing as conservative estimate
        pricing = MODELS["claude-opus-4"]

    input_cost = (input_tokens / 1_000_000) * pricing.input_per_1m
    output_cost = (output_tokens / 1_000_000) * pricing.output_per_1m
    cached_cost = (cached_tokens / 1_000_000) * pricing.cached_input_per_1m

    return {
        "model": pricing.name,
        "provider": pricing.provider,
        "input_cost": round(input_cost, 6),
        "output_cost": round(output_cost, 6),
        "cached_cost": round(cached_cost, 6),
        "total_cost": round(input_cost + output_cost + cached_cost, 6),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
    }


def estimate_savings(
    saved_tokens: int,
    model: str = "claude-opus-4",
) -> dict[str, float]:
    """Estimate dollar savings from avoided input tokens.

    Returns savings for the specified model plus comparisons.
    """
    results = {}
    for key in ("claude-opus-4", "claude-sonnet-4", "gpt-4o", "gemini-2.5-pro"):
        pricing = MODELS.get(key)
        if pricing:
            cost = (saved_tokens / 1_000_000) * pricing.input_per_1m
            results[pricing.name] = round(cost, 6)

    return results


def format_pricing_table() -> str:
    """Format all known model prices as a table."""
    lines = []
    lines.append(f"\n  MODEL PRICING (as of {LAST_UPDATED})")
    lines.append(f"  {'Model':<30} {'Provider':<12} {'Input $/1M':>10} {'Output $/1M':>12} {'Cached':>8} {'Context':>10}")
    lines.append(f"  {'-'*30} {'-'*12} {'-'*10} {'-'*12} {'-'*8} {'-'*10}")

    for key, p in sorted(MODELS.items(), key=lambda x: (-x[1].input_per_1m)):
        cached = f"${p.cached_input_per_1m:.2f}" if p.cached_input_per_1m else "—"
        ctx = f"{p.context_window // 1000}K" if p.context_window else "—"
        lines.append(
            f"  {p.name:<30} {p.provider:<12} ${p.input_per_1m:>8.2f} ${p.output_per_1m:>10.2f} {cached:>8} {ctx:>10}"
        )

    return "\n".join(lines)
