#!/usr/bin/env python3
"""RoutingPolicy: per-band model configuration for Calcifer's router.

Config file: ~/.calcifer/config.json
Usage:
    policy = RoutingPolicy.load()
    policy = RoutingPolicy.load(preset="cheap")
    policy = policy.with_override(band_a="opus")
    policy.save()

Bands:
    A — chat/trivial    (Haiku | Sonnet | Opus)
    B — simple reads    (Direct | Sonnet | Opus)
    C — standard task   (Sonnet | Opus)
    D — hard/multi-step (Opus | Sonnet)
    E — escalated       (Opus | Sonnet)
    Judge               (Opus | Sonnet)
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Optional

from .contracts import Mode

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_PATH = Path.home() / ".calcifer" / "config.json"

# Map user-facing model names → Mode enum
_TO_MODE: dict[str, Mode] = {
    "haiku":  Mode.SONNET,     # No native Haiku mode; Sonnet is cheapest cloud tier
    "sonnet": Mode.SONNET,
    "opus":   Mode.OPUS,
    "direct": Mode.DIRECT,
    "local":  Mode.LOCAL_LOOP,
}

# Valid choices per slot (first item = default for that slot)
BAND_VALID_MODELS: dict[str, list[str]] = {
    "band_a": ["haiku", "sonnet", "opus"],
    "band_b": ["direct", "sonnet", "opus"],
    "band_c": ["sonnet", "opus"],
    "band_d": ["opus", "sonnet"],
    "band_e": ["opus", "sonnet"],
    "judge":  ["opus", "sonnet"],
}

PRESETS: dict[str, dict[str, str]] = {
    "cheap": {
        "band_a": "haiku",
        "band_b": "direct",
        "band_c": "sonnet",
        "band_d": "sonnet",
        "band_e": "sonnet",
        "judge":  "sonnet",
    },
    "balanced": {
        "band_a": "haiku",
        "band_b": "direct",
        "band_c": "sonnet",
        "band_d": "opus",
        "band_e": "opus",
        "judge":  "opus",
    },
    "quality": {
        "band_a": "opus",
        "band_b": "sonnet",
        "band_c": "opus",
        "band_d": "opus",
        "band_e": "opus",
        "judge":  "opus",
    },
    "local": {
        "band_a": "local",
        "band_b": "direct",
        "band_c": "local",
        "band_d": "local",
        "band_e": "local",
        "judge":  "local",
    },
}

_BAND_DESCRIPTIONS = {
    "band_a": "chat / trivial",
    "band_b": "simple reads",
    "band_c": "standard task",
    "band_d": "hard / multi-step",
    "band_e": "escalated",
    "judge":  "judgment",
}


# ---------------------------------------------------------------------------
# RoutingPolicy
# ---------------------------------------------------------------------------

class RoutingPolicy:
    """Translates per-band model names into Mode values the broker uses.

    Immutable by convention — use with_override() to create modified copies.
    """

    def __init__(
        self,
        preset: str = "balanced",
        models: Optional[dict[str, str]] = None,
        local_preferred: bool = False,
    ):
        self.preset = preset
        self.models: dict[str, str] = dict(PRESETS["balanced"])
        if models:
            self.models.update(models)
        self.local_preferred = local_preferred

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, preset: Optional[str] = None) -> "RoutingPolicy":
        """Load from config file; optionally apply a named preset on top."""
        raw = _read_config_file()
        if preset:
            if preset not in PRESETS:
                raise ValueError(f"Unknown preset '{preset}'. Options: {', '.join(PRESETS)}")
            return cls(preset=preset, models=dict(PRESETS[preset]))
        return cls(
            preset=raw.get("preset", "balanced"),
            models=raw.get("models", {}),
            local_preferred=raw.get("local_preferred", False),
        )

    @classmethod
    def from_preset(cls, name: str) -> "RoutingPolicy":
        if name not in PRESETS:
            raise ValueError(f"Unknown preset '{name}'. Options: {', '.join(PRESETS)}")
        return cls(preset=name, models=dict(PRESETS[name]))

    # ------------------------------------------------------------------
    # Override (returns new policy, does not mutate)
    # ------------------------------------------------------------------

    def with_override(self, **kwargs: str) -> "RoutingPolicy":
        """Return a copy with specific slot(s) overridden.

        Example:
            policy.with_override(band_a="opus", judge="sonnet")
        """
        new_models = dict(self.models)
        for slot, model in kwargs.items():
            if slot not in BAND_VALID_MODELS:
                raise ValueError(f"Unknown slot '{slot}'. Valid: {list(BAND_VALID_MODELS)}")
            valid = BAND_VALID_MODELS[slot]
            if model not in valid:
                raise ValueError(f"'{model}' not valid for {slot}. Options: {valid}")
            new_models[slot] = model
        return RoutingPolicy(preset="custom", models=new_models, local_preferred=self.local_preferred)

    # ------------------------------------------------------------------
    # Mode resolution (used by BrokerSession)
    # ------------------------------------------------------------------

    def mode_for_band_a(self) -> Mode:
        return _TO_MODE.get(self.models.get("band_a", "haiku"), Mode.SONNET)

    def mode_for_band_b(self) -> Mode:
        return _TO_MODE.get(self.models.get("band_b", "direct"), Mode.DIRECT)

    def planner_model_for(self, band: str) -> str:
        """Return 'sonnet' or 'opus' — used to pick planner class in BrokerSession."""
        slot = {"C": "band_c", "D": "band_d", "E": "band_e"}.get(band.upper(), "band_c")
        val = self.models.get(slot, "sonnet")
        return "opus" if val == "opus" else "sonnet"

    def judge_model(self) -> str:
        """Return 'sonnet' or 'opus' for the judgment step."""
        val = self.models.get("judge", "opus")
        return "opus" if val in ("opus", "local") else "sonnet"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(self.to_dict(), indent=2))

    def to_dict(self) -> dict:
        return {
            "preset": self.preset,
            "models": dict(self.models),
            "local_preferred": self.local_preferred,
        }

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def show(self) -> str:
        """Human-readable settings table."""
        lines = [
            f"Preset:         {self.preset}",
            f"Config file:    {CONFIG_PATH}",
            f"Local preferred: {self.local_preferred}",
            "",
            f"{'Slot':<10} {'Description':<22} {'Model':<10} {'Valid options'}",
            "-" * 68,
        ]
        for slot, desc in _BAND_DESCRIPTIONS.items():
            model = self.models.get(slot, "?")
            valid = ", ".join(BAND_VALID_MODELS[slot])
            lines.append(f"{slot:<10} {desc:<22} {model:<10} {valid}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"RoutingPolicy(preset={self.preset!r}, models={self.models})"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_config_file() -> dict:
    if not CONFIG_PATH.exists():
        return {"preset": "balanced", "models": dict(PRESETS["balanced"])}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"preset": "balanced", "models": dict(PRESETS["balanced"])}
