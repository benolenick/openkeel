#!/usr/bin/env python3
"""RoutingPolicy: User-configurable router settings.

Lets users customize which model is used for each band/task type.
Supports presets and fine-grained control.
"""

import json
from pathlib import Path
from typing import Optional, Literal
from dataclasses import dataclass, asdict
from openkeel.calcifer.contracts import Mode


@dataclass
class RoutingConfig:
    """Routing configuration."""
    preset: str = "balanced"  # balanced, cheap, quality, local
    models: dict = None  # band_a, band_b, band_c, band_d, judge
    local_preferred: bool = False

    def __post_init__(self):
        if self.models is None:
            self.models = self._default_models()

    @staticmethod
    def _default_models():
        """Default model assignments."""
        return {
            "band_a": "haiku",      # Trivial chat
            "band_b": "direct",     # Simple reads
            "band_c": "sonnet",     # Standard tasks
            "band_d": "opus",       # Hard/design
            "judge": "opus",        # Judgment/escalation
        }

    @staticmethod
    def get_preset(name: str) -> "RoutingConfig":
        """Get a preset configuration."""
        presets = {
            "cheap": RoutingConfig(
                preset="cheap",
                models={
                    "band_a": "haiku",
                    "band_b": "direct",
                    "band_c": "sonnet",
                    "band_d": "sonnet",  # Use Sonnet instead of Opus
                    "judge": "sonnet",
                },
            ),
            "balanced": RoutingConfig(
                preset="balanced",
                models=RoutingConfig._default_models(),
            ),
            "quality": RoutingConfig(
                preset="quality",
                models={
                    "band_a": "opus",
                    "band_b": "opus",
                    "band_c": "opus",
                    "band_d": "opus",
                    "judge": "opus",
                },
            ),
            "local": RoutingConfig(
                preset="local",
                models={
                    "band_a": "gemma4",
                    "band_b": "direct",
                    "band_c": "gemma4",
                    "band_d": "opus",  # Still use Opus for hard tasks
                    "judge": "opus",
                },
                local_preferred=True,
            ),
        }
        return presets.get(name, presets["balanced"])


class RoutingPolicy:
    """Manages routing decisions based on user configuration."""

    CONFIG_FILE = Path.home() / ".calcifer" / "config.json"

    def __init__(self, preset: Optional[str] = None, overrides: Optional[dict] = None):
        """Initialize policy.

        Args:
            preset: preset name (overrides config file)
            overrides: dict of model overrides (e.g., {"band_d": "sonnet"})
        """
        # Load from config file or use default
        if self.CONFIG_FILE.exists():
            self.config = self._load_config()
        else:
            self.config = RoutingConfig.get_preset("balanced")

        # Apply preset if specified
        if preset:
            self.config = RoutingConfig.get_preset(preset)

        # Apply overrides
        if overrides:
            self.config.models.update(overrides)

    def get_model_for_band(self, band: str) -> str:
        """Get the model to use for a band.

        Args:
            band: "a", "b", "c", "d"

        Returns:
            model name: "direct", "haiku", "sonnet", "opus", "gemma4", etc.
        """
        key = f"band_{band}"
        return self.config.models.get(key, "sonnet")

    def get_judge_model(self) -> str:
        """Get the model for judgment/escalation."""
        return self.config.models.get("judge", "opus")

    def should_use_local(self) -> bool:
        """Check if local models are preferred."""
        return self.config.local_preferred

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return {
            "preset": self.config.preset,
            "models": self.config.models,
            "local_preferred": self.config.local_preferred,
        }

    def _load_config(self) -> RoutingConfig:
        """Load config from file."""
        try:
            data = json.loads(self.CONFIG_FILE.read_text())
            return RoutingConfig(
                preset=data.get("preset", "balanced"),
                models=data.get("models", RoutingConfig._default_models()),
                local_preferred=data.get("local_preferred", False),
            )
        except Exception as e:
            print(f"Warning: Failed to load config: {e}. Using balanced preset.")
            return RoutingConfig.get_preset("balanced")

    def save(self) -> None:
        """Save config to file."""
        self.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.CONFIG_FILE.write_text(json.dumps(self.to_dict(), indent=2))

    @staticmethod
    def show_config() -> None:
        """Display current configuration."""
        policy = RoutingPolicy()
        print("\nCalcifer Routing Configuration")
        print("=" * 60)
        print(f"Preset: {policy.config.preset}")
        print(f"Local Preferred: {policy.config.local_preferred}")
        print("\nModel Assignments:")
        for band, model in policy.config.models.items():
            print(f"  {band:10s} → {model}")
        print(f"\nConfig file: {RoutingPolicy.CONFIG_FILE}")
        print()

    @staticmethod
    def show_presets() -> None:
        """Display available presets."""
        print("\nAvailable Presets")
        print("=" * 60)

        presets = {
            "cheap": "Minimize cost: use Sonnet for hard tasks",
            "balanced": "Default: Opus for design, Sonnet for standard",
            "quality": "Maximum quality: use Opus for everything",
            "local": "Prefer local Ollama models when available",
        }

        for name, desc in presets.items():
            print(f"  {name:12s} — {desc}")
        print()
